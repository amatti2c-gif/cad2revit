using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.ExtensibleStorage;

namespace Cad2Revit
{
    /// <summary>
    /// Sincronización por elemento entre la app y el archivo de trabajo de Revit.
    ///
    /// Cada elemento que crea cad2revit guarda (almacenamiento del add-in en el propio elemento):
    ///   - clave: qué es, en qué nivel y dónde está (así se reconoce aunque cambie su id en la app);
    ///   - def:   huella de cómo lo definió la app;
    ///   - estado: huella de cómo quedó en Revit (posición, tipo, alturas).
    /// Al ACTUALIZAR:
    ///   - nuevo en la app                          -> se crea;
    ///   - igual en la app                          -> no se toca (aunque lo hayas cambiado en Revit);
    ///   - cambió en la app y no lo tocaste         -> se actualiza (los muros, en su lugar: cotas y etiquetas siguen);
    ///   - cambió en la app y SÍ lo tocaste         -> no se toca: conflicto en el informe (gana Revit);
    ///   - ya no está en la app y no lo tocaste     -> se elimina;
    ///   - ya no está en la app y lo tocaste        -> se conserva y se informa.
    /// Lo creado a mano en Revit nunca se toca. Para "soltar" un elemento de la app, borra "cad2revit"
    /// de su parámetro Comentarios.
    /// </summary>
    public partial class Constructor
    {
        /// <summary>true = actualizar respetando los cambios hechos en Revit; false = construir/reconstruir.</summary>
        public bool Actualizar { get; set; }

        private static readonly Guid GuidSync = new Guid("3d2f6a90-5b1c-4e7a-9c84-1f6e2a7b5d03");
        private const double ToleranciaPosicion = 0.2;      // m

        private static Schema EsquemaSync()
        {
            var esquema = Schema.Lookup(GuidSync);
            if (esquema != null) return esquema;
            var sb = new SchemaBuilder(GuidSync);
            sb.SetSchemaName("cad2revit_sync");
            sb.SetReadAccessLevel(AccessLevel.Public);
            sb.SetWriteAccessLevel(AccessLevel.Public);
            foreach (var campo in new[] { "tipo", "nivel", "pts", "def", "estado" }) sb.AddSimpleField(campo, typeof(string));
            return sb.Finish();
        }

        /// <summary>¿La importación anterior ya tiene datos de sincronización?</summary>
        public static bool TieneSincronizacion(Document doc)
        {
            var esquema = Schema.Lookup(GuidSync);
            if (esquema == null) return false;
            return ElementosPrevios(doc).Select(doc.GetElement).Any(e => e?.GetEntity(esquema)?.IsValid() == true);
        }

        private class Grupo
        {
            public string Tipo, Nivel, Def;
            public double[][] Pts;
            public readonly List<ElementId> Ids = new();
            public bool Modificado, Usado;
        }

        private readonly List<Grupo> _previos = new();
        private (string tipo, string nivel, string pts, string def)? _actual;
        private readonly List<(ElementId id, string tipo, string nivel, string pts, string def)> _registro = new();
        private int _sCreados, _sActualizados, _sIguales, _sEliminados;
        private readonly List<string> _sConflictos = new(), _sConservados = new();

        // ---------------------------------------------------------------- huellas
        private static string Pts(IEnumerable<double[]> p) =>
            string.Join(";", p.Select(q => string.Format(CultureInfo.InvariantCulture, "{0:0.###},{1:0.###}", q[0], q[1])));

        private static double[][] LeerPts(string s) =>
            (s ?? "").Split(';', StringSplitOptions.RemoveEmptyEntries)
                     .Select(q => q.Split(',').Select(v => double.Parse(v, CultureInfo.InvariantCulture)).ToArray()).ToArray();

        private static string Sha(string texto)
        {
            using var sha = SHA1.Create();
            return Convert.ToHexString(sha.ComputeHash(Encoding.UTF8.GetBytes(texto))).Substring(0, 16);
        }

        /// <summary>Huella de la definición de la app, sin los ids (cambian si se renumera).</summary>
        private static string HuellaDef(object def)
        {
            var nodo = JsonSerializer.SerializeToNode(def);
            void Quitar(JsonNode n)
            {
                if (n is JsonObject o)
                {
                    foreach (var k in o.Select(kv => kv.Key).Where(k => k is "Id" or "Muro" or "Columna").ToList()) o.Remove(k);
                    foreach (var kv in o.ToList()) if (kv.Value != null) Quitar(kv.Value);
                }
                else if (n is JsonArray a) foreach (var x in a) if (x != null) Quitar(x);
            }
            if (nodo != null) Quitar(nodo);
            return Sha(nodo?.ToJsonString() ?? "");
        }

        private static string R(double v) => Math.Round(v, 3).ToString(CultureInfo.InvariantCulture);

        /// <summary>Huella de cómo está el elemento en Revit: si cambia, lo modificaste tú.</summary>
        private string Estado(Element e)
        {
            var sb = new StringBuilder(e.GetTypeId()?.ToString() ?? "");
            switch (e.Location)
            {
                case LocationCurve lc:
                    var c = lc.Curve;
                    foreach (var p in new[] { c.GetEndPoint(0), c.GetEndPoint(1) }) sb.Append($"|{R(p.X)},{R(p.Y)},{R(p.Z)}");
                    break;
                case LocationPoint lp:
                    sb.Append($"|{R(lp.Point.X)},{R(lp.Point.Y)},{R(lp.Point.Z)}|{R(lp.Rotation)}");
                    break;
            }
            BuiltInParameter[] parametros = e is Wall
                ? new[] { BuiltInParameter.WALL_BASE_CONSTRAINT, BuiltInParameter.WALL_HEIGHT_TYPE, BuiltInParameter.WALL_BASE_OFFSET,
                          BuiltInParameter.WALL_TOP_OFFSET, BuiltInParameter.WALL_USER_HEIGHT_PARAM, BuiltInParameter.WALL_STRUCTURAL_SIGNIFICANT }
                : e is FamilyInstance
                    ? new[] { BuiltInParameter.FAMILY_BASE_LEVEL_PARAM, BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM,
                              BuiltInParameter.FAMILY_TOP_LEVEL_PARAM, BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM }
                    : Array.Empty<BuiltInParameter>();
            foreach (var bp in parametros)
            {
                var prm = e.get_Parameter(bp);
                if (prm == null) continue;
                sb.Append('|').Append(prm.StorageType switch
                {
                    StorageType.Double => R(prm.AsDouble()),
                    StorageType.ElementId => prm.AsElementId()?.ToString(),
                    StorageType.Integer => prm.AsInteger().ToString(),
                    _ => prm.AsString(),
                });
            }
            if (e.Location is not LocationCurve && e.Location is not LocationPoint)   // losas, techos, vanos, cielos: su caja
            {
                var bb = e.get_BoundingBox(null);
                if (bb != null) sb.Append($"|{R(bb.Min.X)},{R(bb.Min.Y)},{R(bb.Min.Z)}|{R(bb.Max.X)},{R(bb.Max.Y)},{R(bb.Max.Z)}");
            }
            return Sha(sb.ToString());
        }

        // ---------------------------------------------------------------- lectura de la importación anterior
        private void LeerSincronizacion()
        {
            var esquema = EsquemaSync();
            var grupos = new Dictionary<string, Grupo>();
            foreach (var id in ElementosPrevios(_doc))
            {
                var e = _doc.GetElement(id);
                var ent = e?.GetEntity(esquema);
                if (ent == null || !ent.IsValid()) continue;
                string tipo = ent.Get<string>("tipo"), nivel = ent.Get<string>("nivel"), pts = ent.Get<string>("pts"),
                       def = ent.Get<string>("def"), estado = ent.Get<string>("estado");
                var clave = $"{tipo}|{nivel}|{pts}|{def}";
                if (!grupos.TryGetValue(clave, out var g))
                    grupos[clave] = g = new Grupo { Tipo = tipo, Nivel = nivel, Def = def, Pts = LeerPts(pts) };
                g.Ids.Add(id);
                if (Estado(e) != estado) g.Modificado = true;
            }
            _previos.AddRange(grupos.Values);
        }

        private static bool Cerca(double[][] a, double[][] b)
        {
            if (a.Length != b.Length || a.Length == 0) return false;
            bool Igual(IEnumerable<double[]> x, IEnumerable<double[]> y) => x.Zip(y).All(p => U.Dist(p.First, p.Second) <= ToleranciaPosicion);
            return Igual(a, b) || (a.Length == 2 && Igual(a, b.Reverse()));
        }

        /// <summary>
        /// Decide qué hacer con un elemento (o grupo de elementos) de la app. Devuelve true si hay que crearlo;
        /// en <paramref name="existentes"/> quedan los elementos de Revit que lo representan si se conservan.
        /// <paramref name="enSitio"/>: actualización en su lugar (p. ej. mover un muro) en vez de borrar y crear.
        /// </summary>
        private bool Unidad(string tipo, string nivel, IEnumerable<double[]> puntos, object def, out List<Element> existentes,
                            Func<List<Element>, bool> enSitio = null)
        {
            existentes = new List<Element>();
            var pts = puntos.Where(p => p != null).ToArray();
            var huella = HuellaDef(def);
            _actual = (tipo, nivel ?? "", Pts(pts), huella);
            if (!Actualizar) return true;

            var g = _previos.FirstOrDefault(x => !x.Usado && x.Tipo == tipo && x.Nivel == (nivel ?? "") && Cerca(x.Pts, pts));
            if (g == null) { _sCreados++; return true; }
            g.Usado = true;
            existentes = g.Ids.Select(_doc.GetElement).Where(x => x != null).ToList();
            if (g.Def == huella) { _sIguales++; _actual = null; return false; }
            if (g.Modificado)
            {
                _sConflictos.Add($"{tipo} en {nivel} ({string.Join(", ", existentes.Select(x => x.Id.ToString()))}): cambió en la app " +
                                 "y también lo modificaste en Revit; se dejó tu versión");
                _actual = null;
                return false;
            }
            _sActualizados++;
            if (enSitio != null && existentes.Count > 0)
            {
                var copia = existentes;
                if (Intentar($"{tipo} (actualizar)", () => { if (!enSitio(copia)) throw new Exception("no se pudo en su lugar"); }, false))
                {
                    foreach (var x in existentes) _registro.Add((x.Id, tipo, nivel ?? "", Pts(pts), huella));
                    _actual = null;
                    return false;
                }
            }
            foreach (var x in existentes)                    // borrar y volver a crear
                try { if (_doc.GetElement(x.Id) != null) _doc.Delete(x.Id); } catch { /* ya eliminado */ }
            existentes = new List<Element>();
            return true;
        }

        /// <summary>Puntos que identifican un contorno (losa, cielo, techo): centro y esquinas de su caja.</summary>
        private static IEnumerable<double[]> PuntosContorno(List<double[]> c)
        {
            if (c == null || c.Count == 0) return Array.Empty<double[]>();
            double x0 = c.Min(p => p[0]), y0 = c.Min(p => p[1]), x1 = c.Max(p => p[0]), y1 = c.Max(p => p[1]);
            return new[] { new[] { (x0 + x1) / 2, (y0 + y1) / 2 }, new[] { x0, y0 }, new[] { x1, y1 } };
        }

        // ---------------------------------------------------------------- cierre
        private void CerrarSincronizacion()
        {
            if (Actualizar)
            {
                foreach (var g in _previos.Where(x => !x.Usado))
                {
                    if (g.Modificado)
                    {
                        _sConservados.Add($"{g.Tipo} en {g.Nivel} ({string.Join(", ", g.Ids)}): ya no está en la app, " +
                                          "pero lo modificaste en Revit; se conservó");
                        continue;
                    }
                    foreach (var id in g.Ids)
                        try { if (_doc.GetElement(id) != null) _doc.Delete(id); } catch { /* ya eliminado */ }
                    _sEliminados++;
                }
            }
            _doc.Regenerate();
            var esquema = EsquemaSync();
            foreach (var (id, tipo, nivel, pts, def) in _registro)
            {
                var e = _doc.GetElement(id);
                if (e == null) continue;
                try
                {
                    var ent = new Entity(esquema);
                    ent.Set("tipo", tipo); ent.Set("nivel", nivel); ent.Set("pts", pts); ent.Set("def", def);
                    ent.Set("estado", Estado(e));
                    e.SetEntity(ent);
                }
                catch { /* elementos que no admiten datos del add-in */ }
            }
            if (!Actualizar) return;
            _inf.Contar("Sincronización: nuevos", _sCreados);
            _inf.Contar("Sincronización: actualizados", _sActualizados);
            _inf.Contar("Sincronización: sin cambios (no se tocaron)", _sIguales);
            _inf.Contar("Sincronización: eliminados (ya no están en la app)", _sEliminados);
            if (_sConflictos.Count > 0) _inf.Contar("Sincronización: conflictos (se dejó tu versión de Revit)", _sConflictos.Count);
            foreach (var c in _sConflictos.Take(40)) _inf.Advertencia("Conflicto: " + c);
            foreach (var c in _sConservados.Take(40)) _inf.Advertencia("Conservado: " + c);
        }
    }
}

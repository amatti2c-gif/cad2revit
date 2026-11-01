using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;

namespace Cad2Revit
{
    public partial class Constructor
    {
        // ------------------------------------------------------------ muros
        private void CrearMuros()
        {
            foreach (var m in _m.Muros)
            {
                Intentar(m.Id, () =>
                {
                    var nivel = Nivel(m.Nivel) ?? throw new System.Exception($"nivel '{m.Nivel}' inexistente");
                    var sup = Nivel(m.NivelSuperior);
                    if (!_tiposMuro.TryGetValue(m.Tipo, out var tipo))
                        throw new System.Exception($"tipo de muro '{m.Tipo}' no fue creado");
                    var z = nivel.Elevation;
                    var linea = Line.CreateBound(U.P(m.Inicio, z), U.P(m.Fin, z));
                    double alto = sup != null ? sup.Elevation - z : U.M(m.Altura ?? 2.6);
                    var w = Wall.Create(_doc, linea, tipo.Id, nivel.Id, alto, 0, false, m.Estructural);
                    if (sup != null) U.Fijar(w, BuiltInParameter.WALL_HEIGHT_TYPE, sup.Id);
                    // Comentario con el id de origen para trazabilidad CAD <-> Revit
                    Marcar(w, m.Id);
                    _muros[m.Id] = w;
                    _inf.Contar(m.Estructural ? "Muros estructurales" : "Muros no estructurales");
                });
            }
        }

        // ------------------------------------------------------------ aberturas
        private void CrearAberturas()
        {
            foreach (var a in _m.Aberturas)
            {
                if (!_muros.TryGetValue(a.Muro, out var muro))
                {
                    _inf.Error($"{a.Id}: muro anfitrión '{a.Muro}' no existe");
                    continue;
                }
                if (a.Modo == "familia")
                {
                    if (Intentar(a.Id, () => ColocarFamilia(a, muro), registrarError: false)) continue;
                    // la familia falta o no se pudo colocar: queda el vano (informado en "faltantes")
                }
                Intentar(a.Id, () => ColocarVano(a, muro));
            }
        }

        private void ColocarFamilia(AberturaDef a, Wall muro)
        {
            var cat = a.Categoria == "puerta" ? BuiltInCategory.OST_Doors : BuiltInCategory.OST_Windows;
            var s = Simbolo(cat, a.Familia, a.Tipo, a.Id)
                    ?? throw new System.Exception("familia no disponible (ver faltantes)");
            s = ConMedidas(s, $"{a.Ancho * 100:0}x{a.Alto * 100:0}",
                (BuiltInParameter.FAMILY_WIDTH_PARAM, new[] { "Width", "Ancho", "Anchura" }, a.Ancho),
                (BuiltInParameter.FAMILY_HEIGHT_PARAM, new[] { "Height", "Altura" }, a.Alto));
            ColocarConSimbolo(a, muro, s);
        }

        private ElementId ColocarConSimbolo(AberturaDef a, Wall muro, FamilySymbol s)
        {
            s = Activar(s);
            var nivel = (Level)_doc.GetElement(muro.LevelId);
            var fi = _doc.Create.NewFamilyInstance(U.P(a.Centro, nivel.Elevation), s, muro, nivel, StructuralType.NonStructural);
            if (a.Categoria == "ventana")
                U.Fijar(fi, BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM, U.M(a.Antepecho));
            Marcar(fi, a.Id);
            _inf.Contar(a.Categoria == "puerta" ? "Puertas" : "Ventanas");
            _aberturasCreadas.Add((a.Id, fi.Id, muro.Id));
            return fi.Id;
        }

        private readonly List<(string id, ElementId abertura, ElementId muro)> _aberturasCreadas = new();

        /// <summary>
        /// Comprueba, ya con el modelo regenerado, que cada puerta/ventana quedó alojada en su
        /// muro y que Revit la registra como inserto que corta ese muro. Si no la corta, agrega un
        /// vano rectangular del mismo tamaño para que el muro quede abierto.
        /// </summary>
        /// <summary>
        /// Comprueba que cada puerta/ventana quedó alojada y corta su muro. Si Revit la rechazó al
        /// confirmar, se deja el vano y se informa (sin probar otras familias).
        /// </summary>
        private void VerificarAberturas()
        {
            foreach (var (id, idAb, idMuro) in _aberturasCreadas.ToList())
            {
                var def = _m.Aberturas.First(x => x.Id == id);
                var muro = _doc.GetElement(idMuro) as Wall;
                if (muro == null) continue;
                var fi = _doc.GetElement(idAb) as FamilyInstance;
                if (fi == null)
                {
                    var clave = def.Categoria == "puerta" ? "Puertas" : "Ventanas";
                    if (_inf.Creados.TryGetValue(clave, out var n)) _inf.Creados[clave] = n - 1;
                    _eliminadosPorRevit.TryGetValue(idAb, out var motivo);
                    _inf.Faltante(def.Familia, def.Tipo, clave, id,
                                  $"Revit rechazó la familia en este muro ({motivo?.Trim()}); se dejó el vano");
                    Etapa("Vano " + id, () => Intentar(id + " (vano)", () => ColocarVano(def, muro)));
                    continue;
                }
                bool corta = fi.Host?.Id == idMuro && muro.FindInserts(true, false, true, true).Contains(idAb);
                if (corta) continue;
                Etapa("Vano " + id, () =>
                {
                    if (Intentar(id + " (vano)", () => ColocarVano(def, muro)))
                        _inf.Advertencia($"{id}: la {def.Categoria} no cortaba el muro {def.Muro}; se agregó el vano.");
                });
            }
        }

        private void ColocarVano(AberturaDef a, Wall muro)
        {
            var nivel = (Level)_doc.GetElement(muro.LevelId);
            var centro = U.P(a.Centro, nivel.Elevation);
            var dir = (muro.Location as LocationCurve)?.Curve is Line l ? l.Direction : XYZ.BasisX;
            var p1 = centro - dir * U.M(a.Ancho / 2) + XYZ.BasisZ * U.M(a.Antepecho);
            var p2 = centro + dir * U.M(a.Ancho / 2) + XYZ.BasisZ * U.M(a.Antepecho + a.Alto);
            var o = _doc.Create.NewOpening(muro, p1, p2);
            Marcar(o, a.Id);
            _inf.Contar("Vanos (sin puerta/ventana)");
        }

        // ------------------------------------------------------------ losas y cielos
        private void CrearLosasYCielos()
        {
            foreach (var l in _m.Losas)
            {
                Intentar(l.Id, () =>
                {
                    var nivel = Nivel(l.Nivel) ?? throw new System.Exception($"nivel '{l.Nivel}' inexistente");
                    if (!_tiposLosa.TryGetValue(l.Tipo, out var tipo))
                        throw new System.Exception($"tipo de losa '{l.Tipo}' no fue creado");
                    var est = _m.TiposLosa.FirstOrDefault(t => t.Nombre == l.Tipo)?.Estructural ?? false;
                    var loop = U.Contorno(l.Contorno, nivel.Elevation);
                    var f = Floor.Create(_doc, new List<CurveLoop> { loop }, tipo.Id, nivel.Id, est && !l.Terminacion, null, 0);
                    U.Fijar(f, BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM, U.M(l.Desfase));
                    Marcar(f, l.Id);
                    _inf.Contar(l.Cubierta ? "Cubiertas" : l.Terminacion ? "Pisos de terminación" : "Losas");
                });
            }

            if (_m.Cielos.Count == 0) return;
            var tipoCielo = U.De<CeilingType>(_doc).FirstOrDefault();
            if (tipoCielo == null) { _inf.Advertencia("La plantilla no tiene tipos de cielo; se omiten"); return; }
            foreach (var c in _m.Cielos)
            {
                Intentar("Cielo " + c.Nivel, () =>
                {
                    var nivel = Nivel(c.Nivel) ?? throw new System.Exception($"nivel '{c.Nivel}' inexistente");
                    var loop = U.Contorno(c.Contorno, nivel.Elevation);
                    var ce = Ceiling.Create(_doc, new List<CurveLoop> { loop }, tipoCielo.Id, nivel.Id);
                    U.Fijar(ce, BuiltInParameter.CEILING_HEIGHTABOVELEVEL_PARAM, U.M(c.Altura));
                    Marcar(ce, "cielo " + c.Nivel);
                    _inf.Contar("Cielos");
                });
            }
        }

        // ------------------------------------------------------------ recintos
        private void CrearRecintos()
        {
            foreach (var r in _m.Recintos)
            {
                Intentar("Recinto " + r.Nombre, () =>
                {
                    var nivel = Nivel(r.Nivel) ?? throw new System.Exception($"nivel '{r.Nivel}' inexistente");
                    var room = _doc.Create.NewRoom(nivel, new UV(U.M(r.Punto[0]), U.M(r.Punto[1])));
                    room.Name = U.NombreSeguro(r.Nombre);
                    Marcar(room, "recinto");
                    _inf.Contar("Recintos");
                });
            }
        }
    }
}

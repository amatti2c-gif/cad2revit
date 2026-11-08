using System;
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

        private readonly Dictionary<string, RoofType> _tiposTecho = new();

        /// <summary>
        /// Cubierta inclinada como techo por contorno (FootPrintRoof): cada borde del contorno que
        /// lleva pendiente define la inclinación (dos aguas: los bordes paralelos a la cumbrera).
        /// Luego los muros del piso de abajo se unen al techo para llegar a los frontones.
        /// </summary>
        private void CrearTechoInclinado(LosaDef l, Level nivel)
        {
            var tipo = TipoTecho(l.Tipo);
            var bordes = new CurveArray();
            var extremos = new List<(XYZ a, XYZ b, int i)>();
            for (int i = 0; i < l.Contorno.Count; i++)
            {
                var a = U.P(l.Contorno[i], nivel.Elevation);
                var b = U.P(l.Contorno[(i + 1) % l.Contorno.Count], nivel.Elevation);
                if (a.DistanceTo(b) < 1e-6) continue;
                bordes.Append(Line.CreateBound(a, b));
                extremos.Add((a, b, i));
            }
            if (tipo == null) throw new Exception("no hay tipo de techo");
            var techo = _doc.Create.NewFootPrintRoof(bordes, nivel, tipo, out ModelCurveArray curvas)
                        ?? throw new Exception("Revit no pudo crear el techo con ese contorno");
            if (curvas == null || curvas.Size == 0) throw new Exception("Revit no devolvió los bordes del techo");
            double tangente = Math.Tan(l.Pendiente * Math.PI / 180.0);
            foreach (ModelCurve mc in curvas)
            {
                var c = mc.GeometryCurve;
                XYZ p0 = c.GetEndPoint(0), p1 = c.GetEndPoint(1);
                var borde = extremos.FirstOrDefault(e =>
                    (e.a.IsAlmostEqualTo(p0, 1e-4) && e.b.IsAlmostEqualTo(p1, 1e-4)) ||
                    (e.a.IsAlmostEqualTo(p1, 1e-4) && e.b.IsAlmostEqualTo(p0, 1e-4)));
                bool pendiente = borde.a != null && l.BordesPendiente.Contains(borde.i);
                techo.set_DefinesSlope(mc, pendiente);
                if (pendiente) techo.set_SlopeAngle(mc, tangente);
            }
            Marcar(techo, l.Id);
            _inf.Contar($"Cubiertas inclinadas ({l.Forma})");
            try { UnirMurosAlTecho(techo, l.Nivel); }          // opcional: nunca debe deshacer el techo
            catch (Exception ex) { _inf.Advertencia($"{l.Id}: el techo se creó, pero no se pudieron unir los muros a él ({ex.Message})"); }
        }

        private RoofType TipoTecho(string nombre)
        {
            if (_tiposTecho.TryGetValue(nombre, out var rt)) return rt;
            var seguro = U.NombreSeguro(nombre);
            rt = U.De<RoofType>(_doc).FirstOrDefault(r => r.Name == seguro);
            if (rt == null)
            {
                var basico = U.De<RoofType>(_doc).FirstOrDefault(r => r.GetCompoundStructure() != null)
                             ?? throw new Exception("la plantilla no tiene tipos de techo básico");
                rt = (RoofType)basico.Duplicate(seguro);
                var def = _m.TiposLosa.FirstOrDefault(t => t.Nombre == nombre);
                if (def != null)
                {
                    try { rt.SetCompoundStructure(Estructura(def, rt.GetCompoundStructure())); }
                    catch (Exception ex) { _inf.Advertencia($"Techo {nombre}: se usaron las capas del tipo base ({ex.Message})"); }
                }
                _inf.Contar("Tipos de techo");
            }
            _tiposTecho[nombre] = rt;
            return rt;
        }

        /// <summary>Muros del piso bajo la cubierta: se unen al techo (llegan a los frontones).
        /// Wall.AddAttachment existe desde Revit 2024; se invoca por reflexión para no fallar si cambia.</summary>
        private void UnirMurosAlTecho(RoofBase techo, string nivelTecho)
        {
            var metodo = typeof(Wall).GetMethods().FirstOrDefault(m => m.Name == "AddAttachment" && m.GetParameters().Length == 2
                                                                       && m.GetParameters()[1].ParameterType.IsEnum);
            if (metodo == null) return;
            var tipoUbic = metodo.GetParameters()[1].ParameterType;
            if (!Enum.IsDefined(tipoUbic, "Top")) return;
            var arriba = Enum.Parse(tipoUbic, "Top");
            int unidos = 0;
            foreach (var m in _m.Muros.Where(m => m.NivelSuperior == nivelTecho))
            {
                if (!_muros.TryGetValue(m.Id, out var w)) continue;
                if (Intentar(m.Id + " (unir al techo)", () => metodo.Invoke(w, new object[] { techo.Id, arriba }), false))
                    unidos++;
            }
            if (unidos > 0) _inf.Contar("Muros unidos al techo", unidos);
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
                    if (l.Cubierta && l.Pendiente > 0 && l.BordesPendiente.Count > 0)
                    {
                        // Techo de Revit por contorno; si Revit lo rechaza, se deja la losa plana y se informa
                        if (Intentar(l.Id + " (techo inclinado)", () => CrearTechoInclinado(l, nivel), false)) return;
                        var motivo = _ultimoFallo;
                        // 2º intento: solo los dos bordes con pendiente más largos (el dos aguas del cuerpo principal);
                        // los encuentros de alas o accesos a otra altura a veces Revit no los resuelve
                        var principales = l.BordesPendiente.OrderByDescending(i => U.Dist(l.Contorno[i], l.Contorno[(i + 1) % l.Contorno.Count]))
                                                           .Take(2).ToList();
                        if (principales.Count < l.BordesPendiente.Count)
                        {
                            var simple = new LosaDef { Id = l.Id, Nivel = l.Nivel, Tipo = l.Tipo, Cubierta = true, Forma = l.Forma,
                                                       Pendiente = l.Pendiente, Contorno = l.Contorno, BordesPendiente = principales };
                            if (Intentar(l.Id + " (techo simplificado)", () => CrearTechoInclinado(simple, nivel), false))
                            {
                                _inf.Advertencia($"{l.Id}: el techo a {l.Forma} completo no se pudo crear ({motivo}); se creó con pendiente " +
                                                 "solo en los dos bordes principales. Revisa los encuentros en Revit.");
                                return;
                            }
                        }
                        _inf.Faltante("Techo básico", l.Tipo, "Cubiertas", l.Id,
                                      $"no se pudo crear el techo a {l.Forma} ({l.Pendiente}°): {motivo}; se dejó una losa plana");
                    }
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
                    var room = _doc.Create.NewRoom(nivel, new UV(U.M(r.Punto[0] - U.OrigenX), U.M(r.Punto[1] - U.OrigenY)));
                    room.Name = U.NombreSeguro(r.Nombre);
                    Marcar(room, "recinto");
                    _inf.Contar("Recintos");
                });
            }
        }
    }
}

using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;

namespace Cad2Revit
{
    public partial class Constructor
    {
        /// <summary>
        /// Entramado de madera: cada muro genera UN elemento (DirectShape) que contiene todas sus
        /// piezas como sólidos con el material de la madera. Así el modelo no se llena de cientos
        /// de elementos y se puede ocultar/aislar el entramado de un muro con un clic.
        /// </summary>
        private void CrearEntramados()
        {
            if (_m.Entramados == null || _m.Entramados.Count == 0) return;
            var cat = new ElementId(BuiltInCategory.OST_StructuralFraming);
            if (!DirectShape.IsValidCategoryId(cat, _doc)) cat = new ElementId(BuiltInCategory.OST_GenericModel);

            bool algunaPieza = false;
            foreach (var e in _m.Entramados)
            {
                var modo = e.Revit?.Modo ?? "piezas";
                if (modo == "familias" && EntramadoConFamilias(e)) continue;
                if (modo != "solido")
                {
                    // Por defecto: cada pieza es un elemento propio creado por cad2revit (no requiere
                    // familias en la plantilla) con parámetros para cubicar.
                    EntramadoPorPiezas(e, cat);
                    algunaPieza = true;
                    continue;
                }
                Intentar("Entramado " + e.Muro, () =>
                {
                    var opciones = new SolidOptions(IdMaterial(e.Material), ElementId.InvalidElementId);
                    var normal = new XYZ(-e.Direccion[1], e.Direccion[0], 0);
                    var solidos = new List<GeometryObject>();
                    foreach (var p in e.Piezas)
                    {
                        var s = Pieza(p, normal, opciones);
                        if (s != null) solidos.Add(s);
                    }
                    if (solidos.Count == 0) throw new Exception("ninguna pieza válida");
                    var ds = DirectShape.CreateElement(_doc, cat);
                    ds.ApplicationId = "cad2revit";
                    ds.ApplicationDataId = "entramado " + e.Muro;
                    ds.SetShape(solidos);
                    try { ds.Name = U.NombreSeguro($"Entramado {e.Muro} ({e.Material})"); }
                    catch { /* algunas versiones no permiten nombrar DirectShapes */ }
                    Marcar(ds, $"entramado {e.Muro}: " +
                               string.Join(", ", e.Resumen.Select(kv => $"{kv.Value} {kv.Key}")) +
                               $"; {e.MetrosLineales:0.0} m lineales");
                    _inf.Contar("Piezas de entramado (madera)", solidos.Count);
                });
            }
            if (algunaPieza) Intentar("Tabla de cubicación de madera", () => CrearTablaMadera(cat));
        }

        // ---------------------------------------------------------------- piezas independientes
        private const string P_PIEZA = "C2R Pieza", P_LARGO = "C2R Largo", P_MURO = "C2R Muro";
        private bool _parametrosListos;

        private static readonly Dictionary<string, string> NombreRol = new()
        {
            ["pie_derecho"] = "Pie derecho", ["pie_derecho_corto"] = "Pie derecho corto", ["jamba"] = "Jamba",
            ["solera_inferior"] = "Solera inferior", ["solera_superior"] = "Solera superior",
            ["cadeneta"] = "Cadeneta", ["dintel"] = "Dintel", ["alfeizar"] = "Alféizar",
        };

        private void EntramadoPorPiezas(EntramadoDef e, ElementId cat)
        {
            Intentar("Parámetros de cubicación", () => AsegurarParametros(cat), registrarError: false);
            if (!_parametrosListos)
                _inf.Advertencia($"No se pudieron crear los parámetros de cubicación ({_ultimoFallo}); las piezas se crean igual.");
            var opciones = new SolidOptions(IdMaterial(e.Material), ElementId.InvalidElementId);
            var normal = new XYZ(-e.Direccion[1], e.Direccion[0], 0);
            int n = 0;
            foreach (var p in e.Piezas)
            {
                Intentar($"{e.Muro} {p.Rol}", () =>
                {
                    var solido = Pieza(p, normal, opciones);
                    if (solido == null) return;
                    var ds = DirectShape.CreateElement(_doc, cat);
                    ds.ApplicationId = "cad2revit";
                    ds.ApplicationDataId = $"entramado {e.Muro} {p.Rol}";
                    ds.SetShape(new List<GeometryObject> { solido });
                    var nombre = $"{(NombreRol.TryGetValue(p.Rol, out var r) ? r : p.Rol)} " +
                                 $"{p.Ancho * 1000:0}x{p.Profundidad * 1000:0} {e.Material}";
                    double largo = Math.Sqrt(Math.Pow(p.Fin[0] - p.Inicio[0], 2) + Math.Pow(p.Fin[1] - p.Inicio[1], 2) +
                                             Math.Pow(p.Fin[2] - p.Inicio[2], 2));
                    ds.LookupParameter(P_PIEZA)?.Set(nombre);
                    ds.LookupParameter(P_LARGO)?.Set(U.M(largo));
                    ds.LookupParameter(P_MURO)?.Set(e.Muro);
                    Marcar(ds, $"entramado {e.Muro} {p.Rol}");
                    n++;
                });
            }
            _inf.Contar("Piezas de madera (elementos)", n);
        }

        /// <summary>
        /// Crea (una sola vez por proyecto) los parámetros de proyecto "C2R Pieza", "C2R Largo" y
        /// "C2R Muro" para la categoría de las piezas. Usa un archivo de parámetros compartidos
        /// temporal y restaura el que el usuario tenía configurado.
        /// </summary>
        private void AsegurarParametros(ElementId catId)
        {
            if (_parametrosListos) return;
            var app = _doc.Application;
            var existentes = new HashSet<string>();
            var it = _doc.ParameterBindings.ForwardIterator();
            while (it.MoveNext()) existentes.Add(it.Key.Name);
            var faltan = new[] { (P_PIEZA, SpecTypeId.String.Text), (P_LARGO, SpecTypeId.Length), (P_MURO, SpecTypeId.String.Text) }
                .Where(x => !existentes.Contains(x.Item1)).ToList();
            if (faltan.Count > 0)
            {
                var original = app.SharedParametersFilename;
                var tmp = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "cad2revit_parametros.txt");
                if (!System.IO.File.Exists(tmp)) System.IO.File.WriteAllText(tmp, "");
                try
                {
                    app.SharedParametersFilename = tmp;
                    var archivo = app.OpenSharedParameterFile() ?? throw new Exception("no se pudo abrir el archivo de parámetros");
                    var grupo = archivo.Groups.get_Item("cad2revit") ?? archivo.Groups.Create("cad2revit");
                    var cats = app.Create.NewCategorySet();
                    cats.Insert(Category.GetCategory(_doc, catId));
                    foreach (var (nombre, spec) in faltan)
                    {
                        var def = grupo.Definitions.get_Item(nombre)
                                  ?? grupo.Definitions.Create(new ExternalDefinitionCreationOptions(nombre, spec));
                        _doc.ParameterBindings.Insert(def, app.Create.NewInstanceBinding(cats), GroupTypeId.Data);
                    }
                }
                finally
                {
                    try { if (!string.IsNullOrEmpty(original)) app.SharedParametersFilename = original; } catch { }
                }
            }
            _parametrosListos = true;
        }

        /// <summary>Tabla "cad2revit - Cubicación madera": cantidad y metros lineales por tipo de pieza.</summary>
        private void CrearTablaMadera(ElementId catId)
        {
            const string nombre = "cad2revit - Cubicación madera";
            if (!_parametrosListos || U.De<ViewSchedule>(_doc).Any(v => v.Name == nombre)) return;
            var vs = ViewSchedule.CreateSchedule(_doc, catId);
            vs.Name = nombre;
            var def = vs.Definition;
            ScheduleField Agregar(string n)
            {
                var sf = def.GetSchedulableFields().FirstOrDefault(f => f.GetName(_doc) == n);
                return sf == null ? null : def.AddField(sf);
            }
            var fPieza = Agregar(P_PIEZA);
            def.AddField(ScheduleFieldType.Count);
            var fLargo = Agregar(P_LARGO);
            if (fPieza != null)
            {
                def.AddSortGroupField(new ScheduleSortGroupField(fPieza.FieldId));
                def.AddFilter(new ScheduleFilter(fPieza.FieldId, ScheduleFilterType.HasValue));
            }
            if (fLargo != null) fLargo.DisplayType = ScheduleFieldDisplayType.Totals;
            def.IsItemized = false;
            def.ShowGrandTotal = true;
            def.ShowGrandTotalCount = true;
            _inf.Advertencia($"Se creó la tabla '{nombre}' (Navegador de proyectos → Tablas).");
        }

        private static readonly HashSet<string> RolesVerticales = new() { "pie_derecho", "pie_derecho_corto", "jamba" };
        private static readonly string[] NombresAnchoMadera = { "b", "Width", "Ancho", "Anchura" };
        private static readonly string[] NombresAltoMadera = { "d", "h", "Depth", "Height", "Altura", "Profundidad" };

        /// <summary>
        /// Cada pieza como elemento de Revit: verticales como columnas estructurales y horizontales
        /// como vigas colocadas planas (b = espesor del muro, d = cara vista). Permite tablas de
        /// cantidades. Devuelve false si faltan las familias (se usa entonces el sólido único).
        /// </summary>
        private bool EntramadoConFamilias(EntramadoDef e)
        {
            var nivel = Nivel(e.Nivel);
            if (nivel == null) return false;
            var p0 = e.Piezas.FirstOrDefault();
            if (p0 == null) return true;
            double b = p0.Ancho, h = p0.Profundidad;          // cara vista, espesor
            string sufijo = $"{b * 1000:0}x{h * 1000:0}";

            FamilySymbol sv = null, sh = null;
            bool ok = Intentar($"Familias de entramado ({e.Muro})", () =>
            {
                var fv = e.Revit.FamiliaVertical; var fh = e.Revit.FamiliaHorizontal;
                sv = BuscarExacto(BuiltInCategory.OST_StructuralColumns, fv?.Familia, fv?.Tipo);
                sh = BuscarExacto(BuiltInCategory.OST_StructuralFraming, fh?.Familia, fh?.Tipo);
                if (sv == null || sh == null)
                    throw new Exception($"no están cargadas '{fv?.Familia} : {fv?.Tipo}' (pies derechos) y/o " +
                                        $"'{fh?.Familia} : {fh?.Tipo}' (horizontales)");
                // Columna: b a lo largo del muro, d en el espesor. Viga plana: b en el espesor, d vertical.
                sv = ConMedidas(sv, sufijo, (null, NombresAnchoMadera, b), (null, NombresAltoMadera, h));
                sh = ConMedidas(sh, sufijo + " plana", (null, NombresAnchoMadera, h), (null, NombresAltoMadera, b));
            }, registrarError: false);
            if (!ok)
            {
                _inf.Advertencia($"Entramado {e.Muro}: {_ultimoFallo}. Se construye como un solo elemento por muro.");
                return false;
            }

            var mat = IdMaterial(e.Material);
            double angulo = Math.Atan2(e.Direccion[1], e.Direccion[0]);
            int nv = 0, nh = 0;
            foreach (var p in e.Piezas)
            {
                var a = new XYZ(U.M(p.Inicio[0]), U.M(p.Inicio[1]), U.M(p.Inicio[2]));
                var f = new XYZ(U.M(p.Fin[0]), U.M(p.Fin[1]), U.M(p.Fin[2]));
                bool vertical = RolesVerticales.Contains(p.Rol);
                Intentar($"{e.Muro} {p.Rol}", () =>
                {
                    FamilyInstance fi;
                    if (vertical)
                    {
                        var abajo = a.Z <= f.Z ? a : f; var arriba = a.Z <= f.Z ? f : a;
                        fi = _doc.Create.NewFamilyInstance(new XYZ(abajo.X, abajo.Y, nivel.Elevation), sv, nivel, StructuralType.Column);
                        U.Fijar(fi, BuiltInParameter.FAMILY_BASE_LEVEL_PARAM, nivel.Id);
                        U.Fijar(fi, BuiltInParameter.FAMILY_TOP_LEVEL_PARAM, nivel.Id);
                        U.Fijar(fi, BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM, abajo.Z - nivel.Elevation);
                        U.Fijar(fi, BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM, arriba.Z - nivel.Elevation);
                        if (Math.Abs(angulo) > 1e-9)
                            ElementTransformUtils.RotateElement(_doc, fi.Id,
                                Line.CreateBound(abajo, abajo + XYZ.BasisZ), angulo);
                        nv++;
                    }
                    else
                    {
                        fi = _doc.Create.NewFamilyInstance(Line.CreateBound(a, f), sh, nivel, StructuralType.Beam);
                        // eje de la pieza = centro de la sección; sin uniones automáticas en los extremos
                        var zj = fi.get_Parameter(BuiltInParameter.Z_JUSTIFICATION);
                        if (zj != null && !zj.IsReadOnly) zj.Set((int)ZJustification.Center);
                        StructuralFramingUtils.DisallowJoinAtEnd(fi, 0);
                        StructuralFramingUtils.DisallowJoinAtEnd(fi, 1);
                        nh++;
                    }
                    if (mat != ElementId.InvalidElementId)
                        U.Fijar(fi, BuiltInParameter.STRUCTURAL_MATERIAL_PARAM, mat);
                    Marcar(fi, $"entramado {e.Muro} {p.Rol}");
                });
            }
            _inf.Contar("Entramado: pies derechos/jambas (columnas)", nv);
            _inf.Contar("Entramado: soleras/cadenetas/dinteles (vigas)", nh);
            return true;
        }

        private FamilySymbol BuscarExacto(BuiltInCategory cat, string familia, string tipo)
        {
            if (string.IsNullOrEmpty(familia)) return null;
            var l = new FilteredElementCollector(_doc).OfClass(typeof(FamilySymbol)).OfCategory(cat)
                        .Cast<FamilySymbol>().ToList();
            var s = l.FirstOrDefault(x => x.FamilyName == familia && x.Name == tipo)
                    ?? l.FirstOrDefault(x => x.FamilyName == familia)
                    ?? (CargarDesdeBiblioteca(familia)
                        ? new FilteredElementCollector(_doc).OfClass(typeof(FamilySymbol)).OfCategory(cat)
                              .Cast<FamilySymbol>().FirstOrDefault(x => x.FamilyName == familia)
                        : null);
            return s == null ? null : Activar(s);
        }

        /// <summary>Prisma de sección ancho x profundidad a lo largo del eje inicio→fin.</summary>
        private static Solid Pieza(PiezaDef p, XYZ normalMuro, SolidOptions opciones)
        {
            var a = new XYZ(U.M(p.Inicio[0]), U.M(p.Inicio[1]), U.M(p.Inicio[2]));
            var b = new XYZ(U.M(p.Fin[0]), U.M(p.Fin[1]), U.M(p.Fin[2]));
            var eje = b - a;
            var largo = eje.GetLength();
            if (largo < U.M(0.005)) return null;
            eje = eje.Normalize();
            var n = normalMuro.Normalize();
            var w = eje.CrossProduct(n).Normalize();      // lado "ancho", en el plano del muro
            double hw = U.M(p.Ancho) / 2, hn = U.M(p.Profundidad) / 2;
            var c = new[] { a - w * hw - n * hn, a + w * hw - n * hn, a + w * hw + n * hn, a - w * hw + n * hn };
            var perfil = new CurveLoop();
            for (int i = 0; i < 4; i++) perfil.Append(Line.CreateBound(c[i], c[(i + 1) % 4]));
            return GeometryCreationUtilities.CreateExtrusionGeometry(new List<CurveLoop> { perfil }, eje, largo, opciones);
        }
    }
}

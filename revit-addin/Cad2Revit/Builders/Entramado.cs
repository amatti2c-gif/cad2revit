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

            foreach (var e in _m.Entramados)
            {
                if (e.Revit?.Modo == "familias" && EntramadoConFamilias(e)) continue;
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

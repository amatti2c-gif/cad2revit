using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;

namespace Cad2Revit
{
    public partial class Constructor
    {
        private static readonly HashSet<string> RolesVerticales = new() { "pie_derecho", "pie_derecho_corto", "jamba" };
        private static readonly string[] NombresAnchoMadera = { "b", "Width", "Ancho", "Anchura" };
        private static readonly string[] NombresAltoMadera = { "d", "h", "Depth", "Height", "Altura", "Profundidad" };

        /// <summary>
        /// Entramado de madera con familias estándar de Revit, según lo que define la app:
        ///  - pies derechos, pies derechos cortos y jambas: columna estructural (familia vertical);
        ///  - soleras, cadenetas, dinteles y alféizares: viga plana (familia horizontal).
        /// Las familias se cargan solas desde la biblioteca de Revit; si no existen, ese
        /// entramado no se crea y queda en la sección "faltantes" del informe.
        /// </summary>
        private void CrearEntramados()
        {
            foreach (var e in _m.Entramados ?? new List<EntramadoDef>())
            {
                var md = _m.Muros.FirstOrDefault(x => x.Id == e.Muro);
                if (!Unidad("entramado", e.Nivel, new[] { md?.Inicio, md?.Fin }, e, out _)) continue;
                var nivel = Nivel(e.Nivel);
                var p0 = e.Piezas.FirstOrDefault();
                if (nivel == null || p0 == null) continue;
                double b = p0.Ancho, h = p0.Profundidad;          // cara vista, espesor del muro
                string sufijo = $"{b * 1000:0}x{h * 1000:0}";
                var verticales = e.Piezas.Where(p => RolesVerticales.Contains(p.Rol)).ToList();
                var horizontales = e.Piezas.Where(p => !RolesVerticales.Contains(p.Rol)).ToList();

                FamilySymbol sv = null, sh = null;
                Intentar($"Familias del entramado {e.Muro}", () =>
                {
                    var fv = e.Revit?.FamiliaVertical; var fh = e.Revit?.FamiliaHorizontal;
                    sv = SimboloPorMaterial(BuiltInCategory.OST_StructuralColumns, fv?.Familia, fv?.Tipo,
                        $"{e.Muro} ({verticales.Count} piezas verticales)", e.Material, e.Forma, NombresAnchoMadera, NombresAltoMadera);
                    sh = SimboloPorMaterial(BuiltInCategory.OST_StructuralFraming, fh?.Familia, fh?.Tipo,
                        $"{e.Muro} ({horizontales.Count} piezas horizontales)", e.Material, e.Forma, NombresAnchoMadera, NombresAltoMadera);
                    // Columna: b a lo largo del muro, d en el espesor. Viga plana: b en el espesor, d vertical.
                    if (sv != null) sv = ConMedidas(sv, sufijo, (null, NombresAnchoMadera, b), (null, NombresAltoMadera, h));
                    if (sh != null) sh = ConMedidas(sh, sufijo + " plana", (null, NombresAnchoMadera, h), (null, NombresAltoMadera, b));
                });

                var mat = IdMaterial(e.Material);
                double angulo = Math.Atan2(e.Direccion[1], e.Direccion[0]);
                int nv = 0, nh = 0;
                if (sv != null)
                    foreach (var p in verticales)
                        if (Intentar($"{e.Muro} {p.Rol}", () => ColocarVertical(p, sv, nivel, angulo, mat, e.Muro))) nv++;
                if (sh != null)
                    foreach (var p in horizontales)
                        if (Intentar($"{e.Muro} {p.Rol}", () => ColocarHorizontal(p, sh, nivel, mat, e.Muro))) nh++;
                _inf.Contar("Entramado: piezas verticales", nv);
                _inf.Contar("Entramado: piezas horizontales", nh);
            }
        }

        private void ColocarVertical(PiezaDef p, FamilySymbol s, Level nivel, double angulo, ElementId mat, string muro)
        {
            var a = Punto(p.Inicio); var f = Punto(p.Fin);
            var abajo = a.Z <= f.Z ? a : f; var arriba = a.Z <= f.Z ? f : a;
            var fi = _doc.Create.NewFamilyInstance(new XYZ(abajo.X, abajo.Y, nivel.Elevation), s, nivel, StructuralType.Column);
            U.Fijar(fi, BuiltInParameter.FAMILY_BASE_LEVEL_PARAM, nivel.Id);
            U.Fijar(fi, BuiltInParameter.FAMILY_TOP_LEVEL_PARAM, nivel.Id);
            U.Fijar(fi, BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM, abajo.Z - nivel.Elevation);
            U.Fijar(fi, BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM, arriba.Z - nivel.Elevation);
            if (Math.Abs(angulo) > 1e-9)
                ElementTransformUtils.RotateElement(_doc, fi.Id, Line.CreateBound(abajo, abajo + XYZ.BasisZ), angulo);
            if (mat != ElementId.InvalidElementId) U.Fijar(fi, BuiltInParameter.STRUCTURAL_MATERIAL_PARAM, mat);
            Marcar(fi, $"entramado {muro} {p.Rol}");
        }

        private void ColocarHorizontal(PiezaDef p, FamilySymbol s, Level nivel, ElementId mat, string muro)
        {
            var fi = _doc.Create.NewFamilyInstance(Line.CreateBound(Punto(p.Inicio), Punto(p.Fin)), s, nivel, StructuralType.Beam);
            var zj = fi.get_Parameter(BuiltInParameter.Z_JUSTIFICATION);     // eje de la pieza = centro de la sección
            if (zj != null && !zj.IsReadOnly) zj.Set((int)ZJustification.Center);
            StructuralFramingUtils.DisallowJoinAtEnd(fi, 0);
            StructuralFramingUtils.DisallowJoinAtEnd(fi, 1);
            if (mat != ElementId.InvalidElementId) U.Fijar(fi, BuiltInParameter.STRUCTURAL_MATERIAL_PARAM, mat);
            Marcar(fi, $"entramado {muro} {p.Rol}");
        }

        private static XYZ Punto(double[] p) => U.P3(p);
    }
}

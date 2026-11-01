using System;
using System.Linq;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;

namespace Cad2Revit
{
    public partial class Constructor
    {
        private static readonly string[] NombresB = { "b", "Width", "Ancho", "Anchura" };
        private static readonly string[] NombresH = { "h", "Depth", "Height", "Altura", "Profundidad" };

        private ElementId MaterialEstructural => Material(_m.MaterialEstructural);

        // ------------------------------------------------------------ columnas
        private void CrearColumnas()
        {
            foreach (var c in _m.Columnas)
            {
                Intentar(c.Id, () =>
                {
                    var nivel = Nivel(c.Nivel) ?? throw new Exception($"nivel '{c.Nivel}' inexistente");
                    var sup = Nivel(c.NivelSuperior);
                    var s = Simbolo(BuiltInCategory.OST_StructuralColumns, c.Familia, null, c.Id)
                            ?? throw new Exception($"carga una familia de columna estructural (p.ej. '{c.Familia}')");
                    s = ConMedidas(s, $"{c.B * 100:0}x{c.H * 100:0}",
                        (null, new[] { c.ParametroB }.Concat(NombresB).ToArray(), c.B),
                        (null, new[] { c.ParametroH }.Concat(NombresH).ToArray(), c.H));
                    var p = U.P(c.Centro, nivel.Elevation);
                    var fi = _doc.Create.NewFamilyInstance(p, s, nivel, StructuralType.Column);
                    U.Fijar(fi, BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM, 0.0);
                    if (sup != null)
                    {
                        U.Fijar(fi, BuiltInParameter.FAMILY_TOP_LEVEL_PARAM, sup.Id);
                        U.Fijar(fi, BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM, 0.0);
                    }
                    else
                    {
                        U.Fijar(fi, BuiltInParameter.FAMILY_TOP_LEVEL_PARAM, nivel.Id);
                        U.Fijar(fi, BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM, U.M(c.Altura ?? 2.6));
                    }
                    if (Math.Abs(c.Rotacion) > 1e-6)
                        ElementTransformUtils.RotateElement(_doc, fi.Id, Line.CreateBound(p, p + XYZ.BasisZ),
                            c.Rotacion * Math.PI / 180.0);
                    if (MaterialEstructural != ElementId.InvalidElementId)
                        U.Fijar(fi, BuiltInParameter.STRUCTURAL_MATERIAL_PARAM, MaterialEstructural);
                    fi.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.Set("cad2revit " + c.Id);
                    _columnas[c.Id] = fi;
                    _inf.Contar("Columnas");
                });
            }
        }

        // ------------------------------------------------------------ vigas
        private void CrearVigas()
        {
            foreach (var v in _m.Vigas)
            {
                Intentar(v.Id, () =>
                {
                    var nivel = Nivel(v.Nivel) ?? throw new Exception($"nivel '{v.Nivel}' inexistente");
                    var s = Simbolo(BuiltInCategory.OST_StructuralFraming, v.Familia, null, v.Id)
                            ?? throw new Exception($"carga una familia de viga (p.ej. '{v.Familia}')");
                    s = ConMedidas(s, $"{v.B * 100:0}x{v.H * 100:0}",
                        (null, new[] { v.ParametroB }.Concat(NombresB).ToArray(), v.B),
                        (null, new[] { v.ParametroH }.Concat(NombresH).ToArray(), v.H));
                    var z = nivel.Elevation;
                    var linea = Line.CreateBound(U.P(v.Inicio, z), U.P(v.Fin, z));
                    var fi = _doc.Create.NewFamilyInstance(linea, s, nivel, StructuralType.Beam);
                    if (MaterialEstructural != ElementId.InvalidElementId)
                        U.Fijar(fi, BuiltInParameter.STRUCTURAL_MATERIAL_PARAM, MaterialEstructural);
                    fi.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.Set("cad2revit " + v.Id);
                    _inf.Contar("Vigas");
                });
            }
        }

        // ------------------------------------------------------------ fundaciones
        private void CrearFundaciones()
        {
            var f = _m.Fundaciones;
            if (f.Corridas.Count > 0)
            {
                var baseTipo = U.De<WallFoundationType>(_doc).FirstOrDefault();
                if (baseTipo == null)
                    _inf.Error("La plantilla no tiene tipos de cimentación de muro; se omiten fundaciones corridas");
                foreach (var c in baseTipo == null ? Enumerable.Empty<CorridaDef>() : f.Corridas)
                {
                    Intentar("Fundación " + c.Muro, () =>
                    {
                        if (!_muros.TryGetValue(c.Muro, out var muro))
                            throw new Exception($"muro '{c.Muro}' no existe");
                        var tipo = TipoCorrida(baseTipo, c.Ancho, c.Alto);
                        WallFoundation.Create(_doc, tipo.Id, muro.Id);
                        _inf.Contar("Fundaciones corridas");
                    });
                }
            }

            foreach (var a in f.Aisladas)
            {
                Intentar("Zapata " + a.Columna, () =>
                {
                    var col = _m.Columnas.FirstOrDefault(x => x.Id == a.Columna)
                              ?? throw new Exception($"columna '{a.Columna}' no existe");
                    var nivel = Nivel(col.Nivel);
                    var s = Simbolo(BuiltInCategory.OST_StructuralFoundation, a.Familia, null, "Zapata " + a.Columna)
                            ?? throw new Exception($"carga una familia de zapata aislada (p.ej. '{a.Familia}')");
                    s = ConMedidas(s, $"{a.B * 100:0}x{a.L * 100:0}x{a.H * 100:0}",
                        (null, new[] { "Width", "Ancho", "Anchura" }, a.B),
                        (null, new[] { "Length", "Largo", "Longitud" }, a.L),
                        (null, new[] { "Foundation Thickness", "Thickness", "Espesor de cimentación", "Grosor de cimentación", "Espesor" }, a.H));
                    var fi = _doc.Create.NewFamilyInstance(U.P(a.Centro, nivel.Elevation), s, nivel, StructuralType.Footing);
                    // Cara superior de la zapata = sello + alto
                    ElementTransformUtils.MoveElement(_doc, fi.Id, XYZ.BasisZ * U.M(a.CotaSello + a.H));
                    if (MaterialEstructural != ElementId.InvalidElementId)
                        U.Fijar(fi, BuiltInParameter.STRUCTURAL_MATERIAL_PARAM, MaterialEstructural);
                    _inf.Contar("Zapatas aisladas");
                });
            }
        }

        private WallFoundationType TipoCorrida(WallFoundationType baseTipo, double ancho, double alto)
        {
            var nombre = $"Corrida {ancho * 100:0}x{alto * 100:0} (cad2revit)";
            var t = U.De<WallFoundationType>(_doc).FirstOrDefault(x => x.Name == nombre);
            if (t != null) return t;
            t = (WallFoundationType)baseTipo.Duplicate(nombre);
            if (!U.Fijar(t, U.M(ancho), "Width", "Anchura", "Ancho"))
                _inf.Advertencia($"{nombre}: no se pudo fijar el ancho de la fundación corrida");
            if (!U.Fijar(t, U.M(alto), "Foundation Thickness", "Espesor de cimentación", "Grosor de cimentación"))
                _inf.Advertencia($"{nombre}: no se pudo fijar el espesor de la fundación corrida");
            if (MaterialEstructural != ElementId.InvalidElementId)
                U.Fijar(t, BuiltInParameter.STRUCTURAL_MATERIAL_PARAM, MaterialEstructural);
            return t;
        }
    }
}

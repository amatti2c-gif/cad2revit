using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;

namespace Cad2Revit
{
    public partial class Constructor
    {
        private static readonly string[] NombresBAcero = { "b", "bf", "Width", "Ancho", "Anchura" };
        // "Ht": alto de las familias HSS (tubos rectangulares) de Revit
        private static readonly string[] NombresHAcero = { "d", "h", "Ht", "H", "Depth", "Height", "Altura", "Profundidad" };

        /// <summary>
        /// Marcos de acero (de momento o arriostrados en X) con familias estándar de Revit:
        /// columnas estructurales, vigas y diagonales (riostras). Cada perfil usa el tipo con la
        /// designación pedida si existe; si no, una familia de acero de la misma forma ajustada a
        /// b x h. Lo que no se pueda crear queda en "faltantes" del informe.
        /// </summary>
        private void CrearMarcos()
        {
            foreach (var f in _m.Marcos ?? new List<MarcoDef>())
            {
                var nivel = Nivel(f.Nivel);
                var sup = Nivel(f.NivelSuperior);
                if (nivel == null || sup == null) { _inf.Error($"{f.Id}: niveles '{f.Nivel}'/'{f.NivelSuperior}' inexistentes"); continue; }
                if (!Unidad("marco", f.Nivel, new[] { f.Inicio, f.Fin }, f, out _)) continue;

                FamilySymbol sc = null, sv = null, sd = null;
                Intentar($"Perfiles del marco {f.Id}", () =>
                {
                    sc = PerfilAcero(BuiltInCategory.OST_StructuralColumns, f.Perfiles.GetValueOrDefault("columna"), $"{f.Id} columnas");
                    sv = PerfilAcero(BuiltInCategory.OST_StructuralFraming, f.Perfiles.GetValueOrDefault("viga"), $"{f.Id} vigas");
                    if (f.Sistema == "arriostrado_x")
                        sd = PerfilAcero(BuiltInCategory.OST_StructuralFraming, f.Perfiles.GetValueOrDefault("diagonal"), $"{f.Id} diagonales");
                });
                var mat = IdMaterial(f.Material);
                double angulo = Math.Atan2(f.Direccion[1], f.Direccion[0]);
                int nc = 0, nv = 0, nd = 0;

                if (sc != null)
                    foreach (var c in f.Columnas)
                        if (Intentar($"{f.Id} columna", () =>
                        {
                            var p = U.P(c.Punto, nivel.Elevation);
                            var fi = _doc.Create.NewFamilyInstance(p, sc, nivel, StructuralType.Column);
                            U.Fijar(fi, BuiltInParameter.FAMILY_BASE_LEVEL_PARAM, nivel.Id);
                            U.Fijar(fi, BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM, U.M(c.Z0) - nivel.Elevation);
                            U.Fijar(fi, BuiltInParameter.FAMILY_TOP_LEVEL_PARAM, sup.Id);
                            U.Fijar(fi, BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM, U.M(c.Z1) - sup.Elevation);
                            if (Math.Abs(angulo) > 1e-9)       // alma de la columna en el plano del marco
                                ElementTransformUtils.RotateElement(_doc, fi.Id, Line.CreateBound(p, p + XYZ.BasisZ), angulo);
                            Terminar(fi, mat, f.Id + " columna");
                        })) nc++;

                if (sv != null)
                    foreach (var v in f.Vigas)
                        if (Intentar($"{f.Id} viga", () =>
                        {
                            var fi = _doc.Create.NewFamilyInstance(Line.CreateBound(P3(v.Inicio), P3(v.Fin)), sv, sup, StructuralType.Beam);
                            // símbolo de conexión: de momento (marco rígido) o ninguna (marco arriostrado)
                            int conexion = f.Sistema == "momento" ? 1 : 0;
                            foreach (var bip in new[] { BuiltInParameter.STRUCTURAL_BEAM_START_SUPPORT, BuiltInParameter.STRUCTURAL_BEAM_END_SUPPORT })
                            {
                                var pr = fi.get_Parameter(bip);
                                try { if (pr != null && !pr.IsReadOnly && pr.StorageType == StorageType.Integer) pr.Set(conexion); } catch { }
                            }
                            Terminar(fi, mat, f.Id + " viga");
                        })) nv++;

                if (sd != null)
                    foreach (var d in f.Diagonales)
                        if (Intentar($"{f.Id} diagonal", () =>
                        {
                            var fi = _doc.Create.NewFamilyInstance(Line.CreateBound(P3(d.Inicio), P3(d.Fin)), sd, nivel, StructuralType.Brace);
                            Terminar(fi, mat, f.Id + " diagonal");
                        })) nd++;

                _inf.Contar("Marcos de acero: columnas", nc);
                _inf.Contar("Marcos de acero: vigas", nv);
                if (f.Sistema == "arriostrado_x") _inf.Contar("Marcos de acero: diagonales", nd);
            }
        }

        private void Terminar(FamilyInstance fi, ElementId mat, string id)
        {
            if (mat != ElementId.InvalidElementId) U.Fijar(fi, BuiltInParameter.STRUCTURAL_MATERIAL_PARAM, mat);
            Marcar(fi, id);
        }

        private static XYZ P3(double[] p) => U.P3(p);

        /// <summary>
        /// Tipo de Revit para un perfil: si existe un tipo con la designación pedida (en la familia
        /// indicada o en cualquier familia de acero de esa forma) se usa tal cual; si no, se toma una
        /// familia de acero de la misma forma y se ajustan b y h.
        /// </summary>
        private FamilySymbol PerfilAcero(BuiltInCategory cat, PerfilDef p, string id)
        {
            if (p == null) { _inf.Faltante("(sin perfil)", null, cat.ToString(), id, "el lego no define este perfil"); return null; }
            // 1) designación exacta en cualquier familia de la categoría (p. ej. W310X21)
            if (!string.IsNullOrWhiteSpace(p.Perfil))
            {
                var exacto = new FilteredElementCollector(_doc).OfClass(typeof(FamilySymbol)).OfCategory(cat).Cast<FamilySymbol>()
                    .Where(x => string.Equals(x.Name, p.Perfil.Trim(), StringComparison.OrdinalIgnoreCase))
                    .OrderBy(x => x.FamilyName == p.Familia ? 0 : 1).FirstOrDefault();
                if (exacto != null) return Activar(exacto);
            }
            // 2) familia indicada (si corresponde a la forma pedida) o equivalente del mismo material y
            //    forma, con b x h ajustados
            var familia = p.Familia;
            if (!string.IsNullOrEmpty(familia) && ClaveForma.TryGetValue(p.Forma ?? "", out var propias))
            {
                var nombreFam = familia.ToLowerInvariant();
                bool deOtraForma = ClaveForma.Where(kv => kv.Key != p.Forma && kv.Key != "rectangular")
                                             .Any(kv => kv.Value.Any(k => nombreFam.Contains(k)));
                if (deOtraForma && !propias.Any(k => nombreFam.Contains(k)))
                {
                    _inf.Advertencia($"{id}: la familia '{familia}' no es de forma {p.Forma}; se busca una de forma {p.Forma}.");
                    familia = null;
                }
            }
            var s = SimboloPorMaterial(cat, familia, p.Perfil, id, p.Material, p.Forma, NombresBAcero, NombresHAcero);
            if (s == null) return null;
            if (!string.IsNullOrWhiteSpace(p.Perfil) && string.Equals(s.Name, p.Perfil.Trim(), StringComparison.OrdinalIgnoreCase))
                return s;
            var sufijo = string.IsNullOrWhiteSpace(p.Perfil) ? $"{p.B * 1000:0}x{p.H * 1000:0}" : U.NombreSeguro(p.Perfil.Trim());
            return ConMedidas(s, sufijo, (null, NombresBAcero, p.B), (null, NombresHAcero, p.H));
        }
    }
}

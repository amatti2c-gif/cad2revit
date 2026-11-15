using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;

namespace Cad2Revit
{
    public partial class Constructor
    {
        /// <summary>Comentario de las piezas que crea «Entramado en muros» (no empieza con la marca de importación:
        /// así una reimportación o «Reconstruir todo» no las borra).</summary>
        public const string MarcaEntramadoRevit = "cad2revit-entramado muro ";

        /// <summary>
        /// Entramado de madera para muros dibujados en Revit (p. ej. tipo «marco plataforma»), con la misma lógica de la
        /// app: lee de cada muro su eje, altura, encuentros con otros muros y sus puertas, ventanas y vanos.
        /// Si el muro ya tenía un entramado creado así, se reemplaza.
        /// </summary>
        public Informe EntramadoEnMuros(IList<Wall> muros, ConfEntramado conf)
        {
            double ox = U.OrigenX, oy = U.OrigenY;
            U.OrigenX = U.OrigenY = 0;                        // coordenadas de Revit tal cual
            try
            {
                if (!string.IsNullOrEmpty(conf.Material))
                {
                    var m = new FilteredElementCollector(_doc).OfClass(typeof(Material)).Cast<Material>()
                        .FirstOrDefault(x => x.Name.Equals(conf.Material, StringComparison.OrdinalIgnoreCase))
                        ?? new FilteredElementCollector(_doc).OfClass(typeof(Material)).Cast<Material>()
                        .FirstOrDefault(x => x.Name.IndexOf("pino", StringComparison.OrdinalIgnoreCase) >= 0);
                    if (m != null) _materiales[conf.Material] = m.Id;
                    _m.Materiales.Add(new MaterialDef { Nombre = conf.Material, Clase = "madera" });
                }
                var todos = new FilteredElementCollector(_doc).OfClass(typeof(Wall)).Cast<Wall>()
                    .Where(w => w.Location is LocationCurve lc && lc.Curve is Line).ToList();

                using (var grupo = new TransactionGroup(_doc, "cad2revit: entramado en muros"))
                {
                    grupo.Start();
                    Etapa("Entramado en muros de Revit", () =>
                    {
                        foreach (var w in muros)
                            Intentar($"muro {w.Id}", () => EntramadoDeMuro(w, todos, conf));
                    });
                    grupo.Assimilate();
                }
            }
            finally
            {
                U.OrigenX = ox; U.OrigenY = oy;
            }
            return _inf;
        }

        private static double Mt(double pies) => UnitUtils.ConvertFromInternalUnits(pies, UnitTypeId.Meters);

        private void EntramadoDeMuro(Wall w, List<Wall> todos, ConfEntramado confBase)
        {
            if (!(w.Location is LocationCurve lc) || !(lc.Curve is Line linea))
            {
                _inf.Advertencia($"Muro {w.Id}: no es recto; se omite (el entramado se genera en muros rectos)");
                return;
            }
            var nivel = _doc.GetElement(w.LevelId) as Level
                        ?? throw new Exception("el muro no tiene nivel de base");
            // borrar el entramado anterior de este muro
            var marca = MarcaEntramadoRevit + w.Id;
            var previos = new FilteredElementCollector(_doc).WhereElementIsNotElementType()
                .Where(e => e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.AsString()?.StartsWith(marca + " ") == true)
                .Select(e => e.Id).ToList();
            if (previos.Count > 0) _doc.Delete(previos);

            // geometría del muro (m)
            XYZ pa = linea.GetEndPoint(0), pb = linea.GetEndPoint(1);
            double[] a = { Mt(pa.X), Mt(pa.Y) }, b = { Mt(pb.X), Mt(pb.Y) };
            double largo = Math.Sqrt(Math.Pow(b[0] - a[0], 2) + Math.Pow(b[1] - a[1], 2));
            double ux = (b[0] - a[0]) / largo, uy = (b[1] - a[1]) / largo;
            double baseOff = w.get_Parameter(BuiltInParameter.WALL_BASE_OFFSET)?.AsDouble() ?? 0;
            double zBase = Mt(nivel.Elevation + baseOff);
            double alto;
            var tope = w.get_Parameter(BuiltInParameter.WALL_HEIGHT_TYPE)?.AsElementId();
            if (tope != null && tope != ElementId.InvalidElementId && _doc.GetElement(tope) is Level nivelTope)
                alto = Mt(nivelTope.Elevation + (w.get_Parameter(BuiltInParameter.WALL_TOP_OFFSET)?.AsDouble() ?? 0)) - zBase;
            else
                alto = Mt(w.get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM)?.AsDouble() ?? 0);
            if (alto < 0.3) throw new Exception($"altura del muro {alto:0.00} m");

            // espesor de las piezas: el del núcleo estructural del muro (si no se fijó otro)
            var conf = new ConfEntramado
            {
                B = confBase.B, H = confBase.H, SeparacionPies = confBase.SeparacionPies,
                SeparacionHorizontales = confBase.SeparacionHorizontales, Soleras = confBase.Soleras,
                RefuerzoVanos = confBase.RefuerzoVanos, Material = confBase.Material,
                FamiliaVertical = confBase.FamiliaVertical, FamiliaHorizontal = confBase.FamiliaHorizontal,
            };
            if (conf.H <= 0)
            {
                var cs = w.WallType.GetCompoundStructure();
                var est = cs?.GetLayers().Where(l => l.Function == MaterialFunctionAssignment.Structure).Sum(l => l.Width) ?? 0;
                conf.H = est > 0 ? Mt(est) : Math.Min(Mt(w.Width), 0.14);
            }

            // encuentros: cuánto recortar en cada extremo para terminar en la cara del muro vecino
            double Recorte(double[] p)
            {
                double r = 0;
                foreach (var o in todos)
                {
                    if (o.Id == w.Id || !(o.Location is LocationCurve olc) || !(olc.Curve is Line ol)) continue;
                    var q = new XYZ(U.M(p[0]), U.M(p[1]), ol.GetEndPoint(0).Z);
                    var d = ol.Distance(q);
                    if (Mt(d) <= Mt(o.Width) / 2 + 0.02) r = Math.Max(r, Mt(o.Width) / 2);
                }
                return r;
            }
            double r0 = Recorte(a), r1 = Recorte(b);

            // vanos: puertas, ventanas y aberturas alojadas en el muro
            var vanos = new List<VanoMuro>();
            foreach (var id in w.FindInserts(true, false, true, true))
            {
                var e = _doc.GetElement(id);
                double t0, t1, z0, z1;
                bool puerta = false;
                if (e is Opening op && op.BoundaryRect != null && op.BoundaryRect.Count >= 2)
                {
                    var q0 = op.BoundaryRect[0]; var q1 = op.BoundaryRect[1];
                    double ta = (Mt(q0.X) - a[0]) * ux + (Mt(q0.Y) - a[1]) * uy, tb = (Mt(q1.X) - a[0]) * ux + (Mt(q1.Y) - a[1]) * uy;
                    t0 = Math.Min(ta, tb); t1 = Math.Max(ta, tb);
                    z0 = Mt(Math.Min(q0.Z, q1.Z)); z1 = Mt(Math.Max(q0.Z, q1.Z));
                }
                else if (e is FamilyInstance fi && fi.Location is LocationPoint lp)
                {
                    puerta = fi.Category?.Id == new ElementId(BuiltInCategory.OST_Doors);
                    double tc = (Mt(lp.Point.X) - a[0]) * ux + (Mt(lp.Point.Y) - a[1]) * uy;
                    double Par(BuiltInParameter bip, params string[] nombres)
                    {
                        var p = fi.get_Parameter(bip) ?? fi.Symbol.get_Parameter(bip);
                        if (p == null || p.StorageType != StorageType.Double || p.AsDouble() <= 0)
                            p = nombres.Select(n => fi.LookupParameter(n) ?? fi.Symbol.LookupParameter(n))
                                       .FirstOrDefault(x => x != null && x.StorageType == StorageType.Double && x.AsDouble() > 0);
                        return p != null ? Mt(p.AsDouble()) : 0;
                    }
                    double ancho = Par(BuiltInParameter.FAMILY_ROUGH_WIDTH_PARAM, "Rough Width", "Ancho aproximado");
                    if (ancho <= 0) ancho = Par(BuiltInParameter.FAMILY_WIDTH_PARAM, "Width", "Ancho", "Anchura");
                    double altoV = Par(BuiltInParameter.FAMILY_ROUGH_HEIGHT_PARAM, "Rough Height", "Altura aproximada");
                    if (altoV <= 0) altoV = Par(BuiltInParameter.FAMILY_HEIGHT_PARAM, "Height", "Altura");
                    double antepecho = puerta ? 0 : Mt(fi.get_Parameter(BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)?.AsDouble() ?? 0);
                    if (ancho <= 0 || altoV <= 0)
                    {
                        var bbx = fi.get_BoundingBox(null);
                        if (bbx == null) continue;
                        var ts = new[] { bbx.Min, bbx.Max, new XYZ(bbx.Min.X, bbx.Max.Y, 0), new XYZ(bbx.Max.X, bbx.Min.Y, 0) }
                            .Select(q => (Mt(q.X) - a[0]) * ux + (Mt(q.Y) - a[1]) * uy).ToList();
                        ancho = ancho > 0 ? ancho : ts.Max() - ts.Min();
                        if (altoV <= 0) { antepecho = Mt(bbx.Min.Z) - zBase; altoV = Mt(bbx.Max.Z - bbx.Min.Z); }
                    }
                    t0 = tc - ancho / 2; t1 = tc + ancho / 2;
                    z0 = zBase + antepecho; z1 = z0 + altoV;
                }
                else continue;
                vanos.Add(new VanoMuro { T0 = t0, T1 = t1, ZInf = z0, ZSup = z1, Puerta = puerta });
            }

            var piezas = GeneradorEntramado.Generar(a, b, zBase, zBase + alto, r0, r1, vanos, conf);
            if (piezas.Count == 0) { _inf.Advertencia($"Muro {w.Id}: muy corto para entramado"); return; }

            // familias de madera con la sección pedida (las mismas del entramado de la app)
            string sufijo = $"{conf.B * 1000:0}x{conf.H * 1000:0}";
            var sv = SimboloPorMaterial(BuiltInCategory.OST_StructuralColumns, conf.FamiliaVertical?.Familia, conf.FamiliaVertical?.Tipo,
                                        $"muro {w.Id} (piezas verticales)", conf.Material, "rectangular", NombresAnchoMadera, NombresAltoMadera);
            var sh = SimboloPorMaterial(BuiltInCategory.OST_StructuralFraming, conf.FamiliaHorizontal?.Familia, conf.FamiliaHorizontal?.Tipo,
                                        $"muro {w.Id} (piezas horizontales)", conf.Material, "rectangular", NombresAnchoMadera, NombresAltoMadera);
            if (sv == null || sh == null)
                throw new Exception("no hay familias de madera (Dimension Lumber); cárgalas con Insertar → Cargar familia de Autodesk");
            sv = ConMedidas(sv, sufijo, (null, NombresAnchoMadera, conf.B), (null, NombresAltoMadera, conf.H));
            sh = ConMedidas(sh, sufijo + " plana", (null, NombresAnchoMadera, conf.H), (null, NombresAltoMadera, conf.B));

            var mat = IdMaterial(conf.Material);
            double angulo = Math.Atan2(uy, ux);
            int nv = 0, nh = 0;
            foreach (var p in piezas)
            {
                bool vertical = RolesVerticales.Contains(p.Rol);
                if (Intentar($"muro {w.Id} {p.Rol}", () =>
                {
                    if (vertical) ColocarVertical(p, sv, nivel, angulo, mat, "", $"{marca} {p.Rol}");
                    else ColocarHorizontal(p, sh, nivel, mat, "", $"{marca} {p.Rol}");
                }))
                {
                    if (vertical) nv++; else nh++;
                }
            }
            _inf.Contar("Muros con entramado", 1);
            _inf.Contar("Entramado: piezas verticales", nv);
            _inf.Contar("Entramado: piezas horizontales", nh);
        }
    }
}

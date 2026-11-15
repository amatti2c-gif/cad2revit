using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;

namespace Cad2Revit
{
    public partial class Constructor
    {
        private bool EsPiezaEntramado(Element e)
        {
            var c = e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.AsString() ?? "";
            return c.StartsWith(MarcaEntramadoRevit) || c.StartsWith(Marca + "entramado");
        }

        /// <summary>
        /// Une cada pilar a la viga que llega a su parte superior (también inclinada): quita la unión de geometría que
        /// lo impide y enlaza la parte superior del pilar a la viga, recortando el pilar con la viga.
        /// </summary>
        public Informe UnirPilaresAVigas(IList<FamilyInstance> pilares)
        {
            var vigas = new FilteredElementCollector(_doc).OfClass(typeof(FamilyInstance)).Cast<FamilyInstance>()
                .Where(f => f.StructuralType == StructuralType.Beam && f.Location is LocationCurve lc && lc.Curve is Line && !EsPiezaEntramado(f))
                .ToList();
            double tolPlanta = U.M(0.30), tolAltura = U.M(0.60);
            int unidos = 0, yaEstaban = 0;
            var sinViga = new List<string>();
            var fallidos = new List<string>();
            Etapa("Unir pilares a vigas", () =>
            {
                foreach (var p in pilares)
                {
                    if (!(p.Location is LocationPoint lp)) continue;
                    var caja = p.get_BoundingBox(null);
                    if (caja == null) continue;
                    var tope = new XYZ(lp.Point.X, lp.Point.Y, caja.Max.Z);
                    // la viga más cercana al tope del pilar (en planta pasa por el pilar; en altura, cerca de su tope)
                    FamilyInstance mejor = null; double dm = double.MaxValue;
                    foreach (var v in vigas)
                    {
                        var l = (Line)((LocationCurve)v.Location).Curve;
                        var cv = v.get_BoundingBox(null);
                        if (cv == null || cv.Max.Z < tope.Z - tolAltura || cv.Min.Z > tope.Z + tolAltura) continue;
                        var plano = Line.CreateBound(new XYZ(l.GetEndPoint(0).X, l.GetEndPoint(0).Y, 0), new XYZ(l.GetEndPoint(1).X, l.GetEndPoint(1).Y, 0));
                        if (plano.Length < 1e-6) continue;
                        double d = plano.Distance(new XYZ(tope.X, tope.Y, 0));
                        if (d > tolPlanta) continue;
                        double dz = l.Distance(tope);                      // en 3D: la más cercana al tope
                        if (dz < dm) { dm = dz; mejor = v; }
                    }
                    if (mejor == null) { sinViga.Add(p.Id.ToString()); continue; }
                    try
                    {
                        if (ColumnAttachment.GetColumnAttachment(p, 1) is ColumnAttachment existente && existente.TargetId == mejor.Id)
                        { yaEstaban++; continue; }
                    }
                    catch { /* sin enlace previo */ }
                    var v0 = mejor;
                    bool ok = Intentar($"pilar {p.Id} + viga {v0.Id}", () =>
                    {
                        // Revit vuelve a unir pilar y viga al regenerar: separar, enlazar y volver a separar
                        void Separar()
                        {
                            if (JoinGeometryUtils.AreElementsJoined(_doc, p, v0))
                                JoinGeometryUtils.UnjoinGeometry(_doc, p, v0);
                        }
                        Separar();
                        _doc.Regenerate();
                        Separar();
                        ColumnAttachment.AddColumnAttachment(_doc, p, v0, 1, ColumnAttachmentCutStyle.CutColumn,
                                                             ColumnAttachmentJustification.Maximum, 0.0);
                        _doc.Regenerate();
                        Separar();
                    }, false);
                    if (ok) unidos++; else fallidos.Add($"pilar {p.Id} con viga {v0.Id}: {_ultimoFallo}");
                }
            });
            // comprobar al final: Revit puede quitar el enlace al confirmar
            var hechos = pilares.Where(p => { try { return ColumnAttachment.GetColumnAttachment(p, 1) != null; } catch { return false; } }).ToList();
            if (hechos.Count - yaEstaban < unidos)
                _inf.Advertencia($"Revit quitó {unidos - (hechos.Count - yaEstaban)} enlace(s) al confirmar; esos pilares quedaron sin unir");
            unidos = Math.Max(0, Math.Min(unidos, hechos.Count - yaEstaban));
            _inf.Contar("Pilares unidos a su viga", unidos);
            if (yaEstaban > 0) _inf.Contar("Pilares que ya estaban unidos", yaEstaban);
            if (sinViga.Count > 0) _inf.Advertencia("Sin viga en su parte superior (no se tocaron): pilares " + string.Join(", ", sinViga.Take(30)));
            foreach (var f in fallidos) _inf.Advertencia("No se pudo unir " + f);
            return _inf;
        }
    }
}

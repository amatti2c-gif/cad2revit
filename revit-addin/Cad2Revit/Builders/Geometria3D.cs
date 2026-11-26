using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;

namespace Cad2Revit
{
    /// <summary>
    /// Revisa y corrige la geometría de la estructura elegida para que el modelo de análisis quede conectado tal como se
    /// dibujó: (1) vigas cuya línea de ubicación no coincide con la viga dibujada (Start/End Extension): la línea pasa a
    /// ser la de la viga dibujada y las extensiones quedan en 0; (2) extremos de vigas a menos de 40 cm en planta del eje
    /// de un pilar: se llevan a su eje; (3) extremos de vigas que no llegan a otra viga por menos de 10 cm: se llevan a
    /// ella. Los pilares que no llegan a su viga se unen con «Unir pilares a vigas». Todo en una transacción (Ctrl+Z).
    /// </summary>
    public partial class Constructor
    {
        public class CambioGeometria
        {
            public ElementId Id;
            public string Que;
            public XYZ A, B;                      // nueva línea de la viga
            public bool QuitarExtensiones;
        }

        private static Parameter ParametroExt(Element e, bool inicio)
        {
            var nombres = inicio
                ? new[] { "Start Extension", "Extensión inicial", "Extensión de inicio", "Extension de inicio" }
                : new[] { "End Extension", "Extensión final", "Extensión de fin", "Extension de fin" };
            foreach (var n in nombres)
            {
                var p = e.LookupParameter(n);
                if (p != null && p.StorageType == StorageType.Double) return p;
            }
            return null;
        }

        /// <summary>Propone los cambios (sin tocar el modelo).</summary>
        public List<CambioGeometria> RevisarGeometria(ICollection<ElementId> seleccion)
        {
            var ids = new List<ElementId>();
            var vistos = new HashSet<ElementId>();
            void Agregar(ElementId id0, int prof)
            {
                if (id0 == null || id0 == ElementId.InvalidElementId || !vistos.Add(id0) || prof > 6) return;
                var e0 = _doc.GetElement(id0);
                IEnumerable<ElementId> hijos = e0 is Group g ? g.GetMemberIds() : e0 is AssemblyInstance ai ? ai.GetMemberIds()
                    : e0 is Truss tr ? tr.Members : e0 is BeamSystem bs ? bs.GetBeamIds() : null;
                if (hijos != null) foreach (var h in hijos) Agregar(h, prof + 1);
                else ids.Add(id0);
            }
            foreach (var id in seleccion) Agregar(id, 0);
            var vigas = ids.Select(_doc.GetElement).OfType<FamilyInstance>()
                .Where(f => (f.StructuralType == StructuralType.Beam || f.StructuralType == StructuralType.Brace ||
                             f.Category?.Id.Value == (long)BuiltInCategory.OST_StructuralFraming) &&
                            f.Location is LocationCurve lc && lc.Curve is Line && !EsPiezaEntramado(f)).ToList();
            var pilares = ids.Select(_doc.GetElement).OfType<FamilyInstance>()
                .Where(f => (f.StructuralType == StructuralType.Column || f.Category?.Id.Value == (long)BuiltInCategory.OST_StructuralColumns) &&
                            f.Location is LocationPoint && !EsPiezaEntramado(f)).ToList();
            var cambios = new Dictionary<ElementId, CambioGeometria>();
            var lineas = new Dictionary<ElementId, (XYZ a, XYZ b)>();
            // (1) extensiones: la línea pasa a ser la viga dibujada
            foreach (var v in vigas)
            {
                var l = (Line)((LocationCurve)v.Location).Curve;
                XYZ a = l.GetEndPoint(0), b = l.GetEndPoint(1);
                double e0 = ParametroExt(v, true)?.AsDouble() ?? 0, e1 = ParametroExt(v, false)?.AsDouble() ?? 0;
                if (Math.Abs(e0) > U.M(0.002) || Math.Abs(e1) > U.M(0.002))
                {
                    var (ra, rb) = RecortarAGeometria(v, a, b);
                    cambios[v.Id] = new CambioGeometria
                    {
                        Id = v.Id, A = ra, B = rb, QuitarExtensiones = true,
                        Que = $"viga {v.Id}: línea {Mt(l.Length):0.00} m con extensiones (inicio {Mt(e0):0.00}, fin {Mt(e1):0.00}) → " +
                              $"línea de {Mt(ra.DistanceTo(rb)):0.00} m, la viga dibujada, sin extensiones",
                    };
                    a = ra; b = rb;
                }
                lineas[v.Id] = (a, b);
            }
            // (2) extremos de vigas cerca del eje de un pilar (≤ 40 cm en planta, dentro de su altura ± 40 cm)
            double tolP = U.M(0.40), tolZ = U.M(0.40), casi = U.M(0.002);
            foreach (var v in vigas)
            {
                var (a, b) = lineas[v.Id];
                var ext = new[] { a, b };
                bool movido = false;
                for (int k = 0; k < 2; k++)
                {
                    var q = ext[k];
                    foreach (var p in pilares)
                    {
                        var c = ((LocationPoint)p.Location).Point;
                        var bb = p.get_BoundingBox(null);
                        if (bb == null || q.Z < bb.Min.Z - tolZ || q.Z > bb.Max.Z + tolZ) continue;
                        double dp = Math.Sqrt((q.X - c.X) * (q.X - c.X) + (q.Y - c.Y) * (q.Y - c.Y));
                        if (dp <= tolP && dp > casi)
                        {
                            ext[k] = new XYZ(c.X, c.Y, q.Z);
                            movido = true;
                            break;
                        }
                    }
                }
                if (movido)
                {
                    var prev = cambios.TryGetValue(v.Id, out var cg) ? cg.Que + "; " : "";
                    cambios[v.Id] = new CambioGeometria
                    {
                        Id = v.Id, A = ext[0], B = ext[1], QuitarExtensiones = cg?.QuitarExtensiones ?? false,
                        Que = prev + $"viga {v.Id}: extremo(s) llevados al eje del pilar ({Mt(Math.Max(a.DistanceTo(ext[0]), b.DistanceTo(ext[1]))) * 1000:0} mm)",
                    };
                    lineas[v.Id] = (ext[0], ext[1]);
                }
            }
            // (3) extremos de vigas que no llegan a otra viga (≤ 10 cm): al punto más cercano de esa viga
            double tolV = U.M(0.10);
            foreach (var v in vigas)
            {
                var (a, b) = lineas[v.Id];
                var ext = new[] { a, b };
                bool movido = false;
                for (int k = 0; k < 2; k++)
                {
                    var q = ext[k];
                    double mejor = double.MaxValue; XYZ punto = null;
                    foreach (var o in vigas)
                    {
                        if (o.Id == v.Id) continue;
                        var (oa, ob) = lineas[o.Id];
                        if (oa.DistanceTo(ob) < 1e-6) continue;
                        var lo = Line.CreateBound(oa, ob);
                        var pr = lo.Project(q);
                        if (pr == null) continue;
                        double d = pr.Distance;
                        if (d > casi && d <= tolV && d < mejor) { mejor = d; punto = pr.XYZPoint; }
                        if (d <= casi) { mejor = 0; punto = null; break; }       // ya toca otra viga
                    }
                    if (punto != null) { ext[k] = punto; movido = true; }
                }
                if (movido && ext[0].DistanceTo(ext[1]) > U.M(0.05))
                {
                    var prev = cambios.TryGetValue(v.Id, out var cg) ? cg.Que + "; " : "";
                    cambios[v.Id] = new CambioGeometria
                    {
                        Id = v.Id, A = ext[0], B = ext[1], QuitarExtensiones = cg?.QuitarExtensiones ?? false,
                        Que = prev + $"viga {v.Id}: extremo(s) llevados a la viga que tocan ({Mt(Math.Max(a.DistanceTo(ext[0]), b.DistanceTo(ext[1]))) * 1000:0} mm)",
                    };
                    lineas[v.Id] = (ext[0], ext[1]);
                }
            }
            return cambios.Values.ToList();
        }

        /// <summary>Aplica los cambios propuestos y une los pilares elegidos a sus vigas.</summary>
        public Informe CorregirGeometria(List<CambioGeometria> cambios, IList<FamilyInstance> pilares)
        {
            int hechos = 0;
            Etapa("Corregir geometría de la estructura", () =>
            {
                foreach (var c in cambios)
                {
                    var v = _doc.GetElement(c.Id) as FamilyInstance;
                    if (v == null) continue;
                    bool ok = Intentar($"viga {c.Id}", () =>
                    {
                        if (c.QuitarExtensiones)
                        {
                            foreach (var p in new[] { ParametroExt(v, true), ParametroExt(v, false) })
                                if (p != null && !p.IsReadOnly) p.Set(0.0);
                            _doc.Regenerate();
                        }
                        ((LocationCurve)v.Location).Curve = Line.CreateBound(c.A, c.B);
                    }, false);
                    if (ok) hechos++;
                    else _inf.Advertencia($"No se pudo corregir la viga {c.Id}: {_ultimoFallo}");
                }
            });
            _inf.Contar("Vigas corregidas", hechos);
            foreach (var c in cambios.Take(40)) _inf.Advertencia(c.Que);
            if (pilares.Count > 0) UnirPilaresAVigas(pilares);
            return _inf;
        }
    }
}

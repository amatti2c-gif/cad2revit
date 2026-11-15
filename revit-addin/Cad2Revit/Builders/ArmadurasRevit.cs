using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;

namespace Cad2Revit
{
    /// <summary>Armaduras de un sobrecimiento (pedestal + fundación corrida), como las lee la app del croquis (mm y cm).</summary>
    public class ArmaduraPedestal
    {
        public int BarrasPorCara { get; set; } = 2;
        public double DiametroMm { get; set; } = 12;
        public double EstriboDiametroMm { get; set; } = 8;
        public double EstriboSeparacionCm { get; set; } = 20;
        public bool EstriboHastaFondoFundacion { get; set; } = true;
    }

    public class ArmaduraFundacion
    {
        public int BarrasSuperiores { get; set; } = 4;
        public int BarrasInferiores { get; set; } = 4;
        public double DiametroMm { get; set; } = 12;
        public double EstriboDiametroMm { get; set; } = 8;
        public double EstriboSeparacionCm { get; set; } = 20;
    }

    public class LecturaArmaduras
    {
        public ArmaduraPedestal Pedestal { get; set; } = new();
        public ArmaduraFundacion Fundacion { get; set; } = new();
        public int TotalBarrasLongitudinales { get; set; }
        public string Observaciones { get; set; } = "";
        public double RecubrimientoPedestalCm { get; set; } = 3;
        public double RecubrimientoFundacionCm { get; set; } = 5;
    }

    public partial class Constructor
    {
        public const string MarcaArmadura = "cad2revit-armadura muro ";

        /// <summary>
        /// Crea las armaduras del croquis: barras longitudinales a lo largo del muro (pedestal) y de su zapata corrida
        /// (fundación), y estribos cerrados en toda su longitud. Si el muro ya tenía armaduras hechas con este botón,
        /// se reemplazan.
        /// </summary>
        public Informe ArmadurasPedestalFundacion(Wall muro, Element fundacion, LecturaArmaduras a)
        {
            double M(double metros) => U.M(metros);
            if (!(muro.Location is LocationCurve lc) || !(lc.Curve is Line linea))
            {
                _inf.Error("El muro del pedestal no es recto");
                return _inf;
            }
            // marco local: u a lo largo del muro, n perpendicular horizontal, z vertical (en pies)
            XYZ p0 = linea.GetEndPoint(0), p1 = linea.GetEndPoint(1);
            var u = new XYZ(p1.X - p0.X, p1.Y - p0.Y, 0).Normalize();
            var n = XYZ.BasisZ.CrossProduct(u);
            var o = new XYZ(p0.X, p0.Y, 0);
            (double t0, double t1, double n0, double n1, double z0, double z1) Caja(Element e)
            {
                var pts = PuntosGeometria(e);
                if (pts.Count == 0) throw new Exception($"el elemento {e.Id} no tiene geometría");
                return (pts.Min(q => (q - o).DotProduct(u)), pts.Max(q => (q - o).DotProduct(u)),
                        pts.Min(q => (q - o).DotProduct(n)), pts.Max(q => (q - o).DotProduct(n)),
                        pts.Min(q => q.Z), pts.Max(q => q.Z));
            }
            var ped = Caja(muro);
            var fun = Caja(fundacion);
            if (fun.z1 > ped.z0 + M(0.05) || fun.n1 - fun.n0 < ped.n1 - ped.n0 - M(0.01))
                _inf.Advertencia("La fundación no queda bajo el muro o es más angosta que él: revisa que elegiste los elementos correctos");

            double cp = M(a.RecubrimientoPedestalCm / 100), cf = M(a.RecubrimientoFundacionCm / 100);
            double dLp = M(a.Pedestal.DiametroMm / 1000), dEp = M(a.Pedestal.EstriboDiametroMm / 1000);
            double dLf = M(a.Fundacion.DiametroMm / 1000), dEf = M(a.Fundacion.EstriboDiametroMm / 1000);
            // largo común a pedestal y fundación, con recubrimiento en los extremos
            double tIni = Math.Max(ped.t0, fun.t0) + cf, tFin = Math.Min(ped.t1, fun.t1) - cf;
            if (tFin - tIni < M(0.3)) { _inf.Error("El muro y la fundación casi no se superponen a lo largo"); return _inf; }
            double zt = fun.z1, zb = fun.z0, zp = ped.z1;

            XYZ P(double t, double nn, double z) => o + u * t + n * nn + XYZ.BasisZ * z;

            // ---- posiciones (n, z) de las barras longitudinales ----
            var largas = new List<(double nn, double z, double d, string rol)>();
            double npI = ped.n0 + cp + dEp + dLp / 2, npD = ped.n1 - cp - dEp - dLp / 2;
            double zFunSup = zt - cf - dEf - dLf / 2, zFunInf = zb + cf + dEf + dLf / 2;
            double zPedSup = zp - cp - dEp - dLp / 2;
            int k = Math.Max(1, a.Pedestal.BarrasPorCara);
            for (int i = 0; i < k; i++)
            {
                double z = zPedSup - i * (zPedSup - zFunSup) / k;      // parejas hasta la capa superior de la fundación
                largas.Add((npI, z, dLp, "pedestal"));
                largas.Add((npD, z, dLp, "pedestal"));
            }
            double nfI = fun.n0 + cf + dEf + dLf / 2, nfD = fun.n1 - cf - dEf - dLf / 2;
            List<double> Capa(int m)
            {
                var xs = new List<double>();
                if (m <= 0) return xs;
                if (m == 1) return new List<double> { (nfI + nfD) / 2 };
                xs.Add(nfI); xs.Add(nfD);
                if (m >= 4 && npI > nfI + dLf && npD < nfD - dLf) { xs.Add(npI); xs.Add(npD); }  // bajo las caras del pedestal
                while (xs.Count < m)
                {
                    var orden = xs.OrderBy(x => x).ToList();
                    int g = Enumerable.Range(0, orden.Count - 1).OrderByDescending(j => orden[j + 1] - orden[j]).First();
                    xs.Add((orden[g] + orden[g + 1]) / 2);                 // en el hueco más grande
                }
                return xs.OrderBy(x => x).ToList();
            }
            foreach (var x in Capa(a.Fundacion.BarrasSuperiores)) largas.Add((x, zFunSup, dLf, "fundación superior"));
            foreach (var x in Capa(a.Fundacion.BarrasInferiores)) largas.Add((x, zFunInf, dLf, "fundación inferior"));

            // ---- estribos (rectángulos en el plano n-z) ----
            var estribos = new List<(double nA, double nB, double zA, double zB, double d, double s, string rol, Element host)>();
            double zEpInf = a.Pedestal.EstriboHastaFondoFundacion ? zb + cf + dEf + dEp / 2 : zt - cf - dEf - dEp / 2;
            estribos.Add((ped.n0 + cp + dEp / 2, ped.n1 - cp - dEp / 2, zEpInf, zp - cp - dEp / 2, dEp,
                          M(a.Pedestal.EstriboSeparacionCm / 100), "estribo pedestal", muro));
            estribos.Add((fun.n0 + cf + dEf / 2, fun.n1 - cf - dEf / 2, zb + cf + dEf / 2, zt - cf - dEf / 2, dEf,
                          M(a.Fundacion.EstriboSeparacionCm / 100), "estribo fundación", fundacion));

            var marca = MarcaArmadura + muro.Id;
            bool fundacionAloja = RebarHostData.IsValidHost(fundacion);
            if (!RebarHostData.IsValidHost(muro))
            {
                _inf.Error("Revit no acepta armaduras en este muro: debe ser estructural y de hormigón (revisa su tipo y material)");
                return _inf;
            }
            if (!fundacionAloja)
                _inf.Advertencia("La fundación no acepta armaduras: sus barras quedan alojadas en el muro del pedestal");

            int nLargas = 0, nEstribos = 0;
            Etapa("Armaduras de pedestal y fundación", () =>
            {
                var previas = new FilteredElementCollector(_doc).OfClass(typeof(Rebar))
                    .Where(e => e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.AsString()?.StartsWith(marca + " ") == true)
                    .Select(e => e.Id).ToList();
                if (previas.Count > 0) _doc.Delete(previas);

                var ganchoEstribo = new FilteredElementCollector(_doc).OfClass(typeof(RebarHookType)).Cast<RebarHookType>()
                    .Where(h => h.Style == RebarStyle.StirrupTie)
                    .OrderBy(h => Math.Abs(h.HookAngle - Math.PI * 135 / 180)).FirstOrDefault();

                foreach (var (nn, z, d, rol) in largas)
                {
                    var host = rol == "pedestal" || !fundacionAloja ? (Element)muro : fundacion;
                    if (Intentar($"barra {rol}", () =>
                    {
                        var tipo = TipoBarra(d);
                        var curva = new List<Curve> { Line.CreateBound(P(tIni, nn, z), P(tFin, nn, z)) };
                        var r = Barra(RebarStyle.Standard, tipo, host, n, curva, null);
                        r.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.Set($"{marca} {rol}");
                    })) nLargas++;
                }
                foreach (var (nA, nB, zA, zB, d, s, rol, hostE) in estribos)
                {
                    var host = hostE.Id == fundacion.Id && !fundacionAloja ? (Element)muro : hostE;
                    if (Intentar(rol, () =>
                    {
                        var tipo = TipoBarra(d);
                        var q = new[] { P(tIni, nA, zA), P(tIni, nB, zA), P(tIni, nB, zB), P(tIni, nA, zB) };
                        var curvas = new List<Curve>();
                        for (int i = 0; i < 4; i++) curvas.Add(Line.CreateBound(q[i], q[(i + 1) % 4]));
                        Rebar r = null;
                        if (ganchoEstribo != null)
                        {
                            try
                            {
                                r = Barra(RebarStyle.StirrupTie, tipo, host, u, curvas, ganchoEstribo);
                            }
                            catch { r = null; }                       // sin ganchos de 135° si no calzan
                        }
                        r ??= Barra(RebarStyle.StirrupTie, tipo, host, u, curvas, null);
                        r.GetShapeDrivenAccessor().SetLayoutAsMaximumSpacing(s, tFin - tIni, true, true, true);
                        r.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.Set($"{marca} {rol}");
                    })) nEstribos++;
                }
            });
            _inf.Contar("Barras longitudinales", nLargas);
            _inf.Contar("Estribos (grupos a lo largo del muro)", nEstribos);
            _inf.Advertencia($"Pedestal: {2 * k} φ{a.Pedestal.DiametroMm:0} + E φ{a.Pedestal.EstriboDiametroMm:0}@{a.Pedestal.EstriboSeparacionCm:0}" +
                             (a.Pedestal.EstriboHastaFondoFundacion ? " (hasta el fondo de la fundación)" : ""));
            _inf.Advertencia($"Fundación: {a.Fundacion.BarrasSuperiores} φ{a.Fundacion.DiametroMm:0} sup. + {a.Fundacion.BarrasInferiores} " +
                             $"φ{a.Fundacion.DiametroMm:0} inf. + E φ{a.Fundacion.EstriboDiametroMm:0}@{a.Fundacion.EstriboSeparacionCm:0}");
            _inf.Advertencia($"Recubrimientos: pedestal {a.RecubrimientoPedestalCm:0.#} cm, fundación {a.RecubrimientoFundacionCm:0.#} cm; " +
                             $"largo armado {Mt(tFin - tIni):0.00} m");
            return _inf;
        }

        private Rebar Barra(RebarStyle estilo, RebarBarType tipo, Element host, XYZ normal, IList<Curve> curvas, RebarHookType gancho)
        {
            using var term = new BarTerminationsData(_doc);
            if (gancho != null)
            {
                term.HookTypeIdAtStart = gancho.Id;
                term.HookTypeIdAtEnd = gancho.Id;
                term.TerminationOrientationAtStart = RebarTerminationOrientation.Right;
                term.TerminationOrientationAtEnd = RebarTerminationOrientation.Right;
            }
            return Rebar.CreateFromCurves(_doc, estilo, tipo, host, normal, curvas, term, true, true);
        }

        /// <summary>Tipo de barra con ese diámetro (pies); si no hay, se crea «φ{mm}».</summary>
        private RebarBarType TipoBarra(double d)
        {
            var tipos = new FilteredElementCollector(_doc).OfClass(typeof(RebarBarType)).Cast<RebarBarType>().ToList();
            var t = tipos.FirstOrDefault(x => Math.Abs(x.BarNominalDiameter - d) < U.M(0.0002));
            if (t != null) return t;
            string nombre = $"φ{Mt(d) * 1000:0}";
            t = tipos.FirstOrDefault(x => x.Name == nombre);
            if (t != null) return t;
            t = RebarBarType.Create(_doc);
            t.BarNominalDiameter = d;
            t.BarModelDiameter = d;
            t.StandardBendDiameter = 6 * d;
            t.StirrupTieBendDiameter = 4 * d;
            t.StandardHookBendDiameter = 6 * d;
            try { t.Name = nombre; } catch { }
            _inf.Advertencia($"Se creó el tipo de barra {nombre}");
            return t;
        }
    }
}

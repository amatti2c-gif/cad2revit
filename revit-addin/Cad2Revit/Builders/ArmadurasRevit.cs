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
            // Cada barra queda POR DENTRO del estribo que la abraza: en los tramos rectos, tocando la cara interior del
            // estribo; en las esquinas, dentro de la vuelta (tocando el doblez, cuyo radio interior es la mitad del
            // diámetro de doblado del estribo).
            double RadioInterior(double dE)
            {
                var t = BuscarTipoBarra(dE);
                return (t != null && t.StirrupTieBendDiameter > 0 ? t.StirrupTieBendDiameter : 4 * dE) / 2;
            }
            double Esquina(double dL, double rIn) => rIn > dL / 2 ? rIn - (rIn - dL / 2) / Math.Sqrt(2) : dL / 2;
            double rEp = RadioInterior(dEp), rEf = RadioInterior(dEf);
            // caras interiores de los estribos
            double pI0 = ped.n0 + cp + dEp, pI1 = ped.n1 - cp - dEp, pZ1 = zp - cp - dEp;
            double pZ0 = a.Pedestal.EstriboHastaFondoFundacion ? zb + cf + dEf + dEp : zt - cf - dEf;
            double fI0 = fun.n0 + cf + dEf, fI1 = fun.n1 - cf - dEf, fZ0 = zb + cf + dEf, fZ1 = zt - cf - dEf;

            var largas = new List<(double nn, double z, double d, string rol)>();
            double eP = Esquina(dLp, rEp);
            double zPedSup = pZ1 - eP, zFunSup = fZ1 - dLf / 2;
            int k = Math.Max(1, a.Pedestal.BarrasPorCara);
            for (int i = 0; i < k; i++)
            {
                double z = zPedSup - i * (zPedSup - zFunSup) / k;      // parejas hasta la capa superior de la fundación
                double off = i == 0 ? eP : dLp / 2;                     // la de arriba, en la vuelta del estribo
                largas.Add((pI0 + off, z, dLp, "pedestal"));
                largas.Add((pI1 - off, z, dLp, "pedestal"));
            }
            double eF = Esquina(dLf, rEf), ePf = Esquina(dLf, rEp);
            bool bajoPedestal = pI0 > fI0 + 2 * dLf && pI1 < fI1 - 2 * dLf;
            List<(double nn, double z)> Capa(int m, bool superior)
            {
                double zRecta = superior ? fZ1 - dLf / 2 : fZ0 + dLf / 2;
                double zEsq = superior ? fZ1 - eF : fZ0 + eF;
                var xs = new List<(double nn, double z)>();
                if (m <= 0) return xs;
                if (m == 1) return new List<(double, double)> { ((fI0 + fI1) / 2, zRecta) };
                xs.Add((fI0 + eF, zEsq)); xs.Add((fI1 - eF, zEsq));                    // esquinas del estribo de la fundación
                if (m >= 4 && bajoPedestal)
                {
                    if (superior)                                                        // dentro de las ramas del estribo del pedestal
                    { xs.Add((pI0 + dLf / 2, zRecta)); xs.Add((pI1 - dLf / 2, zRecta)); }
                    else if (a.Pedestal.EstriboHastaFondoFundacion)                     // en las vueltas inferiores del estribo del pedestal
                    { xs.Add((pI0 + ePf, pZ0 + ePf)); xs.Add((pI1 - ePf, pZ0 + ePf)); }
                    else
                    { xs.Add((pI0 + dLf / 2, zRecta)); xs.Add((pI1 - dLf / 2, zRecta)); }
                }
                while (xs.Count < m)
                {
                    var orden = xs.OrderBy(x => x.nn).ToList();
                    int g = Enumerable.Range(0, orden.Count - 1).OrderByDescending(j2 => orden[j2 + 1].nn - orden[j2].nn).First();
                    xs.Add(((orden[g].nn + orden[g + 1].nn) / 2, zRecta));                // en el hueco más grande
                }
                return xs.OrderBy(x => x.nn).ToList();
            }
            foreach (var (x, z) in Capa(a.Fundacion.BarrasSuperiores, true)) largas.Add((x, z, dLf, "fundación superior"));
            foreach (var (x, z) in Capa(a.Fundacion.BarrasInferiores, false)) largas.Add((x, z, dLf, "fundación inferior"));

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
                        // empieza y termina en una esquina SUPERIOR (ahí van los ganchos), con el mismo sentido de giro
                        var q = new[] { P(tIni, nA, zB), P(tIni, nA, zA), P(tIni, nB, zA), P(tIni, nB, zB) };
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
                // ganchos de 135° doblados hacia el interior del estribo (hacia el núcleo de hormigón)
                term.TerminationOrientationAtStart = RebarTerminationOrientation.Left;
                term.TerminationOrientationAtEnd = RebarTerminationOrientation.Left;
            }
            return Rebar.CreateFromCurves(_doc, estilo, tipo, host, normal, curvas, term, true, true);
        }

        private RebarBarType BuscarTipoBarra(double d)
        {
            var tipos = new FilteredElementCollector(_doc).OfClass(typeof(RebarBarType)).Cast<RebarBarType>().ToList();
            string nombre = $"φ{Mt(d) * 1000:0}";
            return tipos.FirstOrDefault(x => Math.Abs(x.BarNominalDiameter - d) < U.M(0.0002))
                   ?? tipos.FirstOrDefault(x => x.Name == nombre);
        }

        /// <summary>Tipo de barra con ese diámetro (pies); si no hay, se crea «φ{mm}».</summary>
        private RebarBarType TipoBarra(double d)
        {
            var t = BuscarTipoBarra(d);
            if (t != null) return t;
            string nombre = $"φ{Mt(d) * 1000:0}";
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

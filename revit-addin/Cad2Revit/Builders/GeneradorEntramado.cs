using System;
using System.Collections.Generic;
using System.Linq;

namespace Cad2Revit
{
    /// <summary>Parámetros del entramado (los mismos del lego de tabique de madera de la app).</summary>
    public class ConfEntramado
    {
        public double B = 0.05;                 // cara vista de la pieza (a lo largo del muro), m
        public double H = 0.076;                // espesor de la pieza (en el espesor del muro), m; 0 = el del núcleo del muro
        public double SeparacionPies = 0.40;
        public double SeparacionHorizontales = 0.40;
        public bool Soleras = true;
        public bool RefuerzoVanos = true;
        public string Material = "Pino radiata";
        public FamiliaTipoDef FamiliaVertical = new() { Familia = "Dimension Lumber-Column", Tipo = "2x3" };
        public FamiliaTipoDef FamiliaHorizontal = new() { Familia = "Dimension Lumber", Tipo = "2x3" };
    }

    /// <summary>Vano de un muro en sus coordenadas: t a lo largo del eje (m desde el inicio), z absolutas (m).</summary>
    public struct VanoMuro
    {
        public double T0, T1, ZInf, ZSup;
        public bool Puerta;
    }

    /// <summary>
    /// Generador del entramado de madera, igual al de la app (parser/cad2revit/entramado.py): pies derechos,
    /// soleras, cadenetas, jambas, dinteles y alféizares. Todo en metros; el muro va de A a B en planta.
    /// </summary>
    public static class GeneradorEntramado
    {
        private const double Tol = 1e-6;

        private struct Vertical { public double T, Z0, Z1; public string Rol; }

        public static List<PiezaDef> Generar(double[] a, double[] b, double zBase, double zTope, double recorte0,
                                             double recorte1, List<VanoMuro> aberturas, ConfEntramado c)
        {
            var piezas = new List<PiezaDef>();
            double largo = Math.Sqrt(Math.Pow(b[0] - a[0], 2) + Math.Pow(b[1] - a[1], 2));
            if (largo < 1e-6) return piezas;
            double ux = (b[0] - a[0]) / largo, uy = (b[1] - a[1]) / largo;
            double bb = c.B, hh = c.H;
            double s0 = recorte0, s1 = largo - recorte1;
            if (s1 - s0 < 2 * bb) return piezas;
            double sol = c.Soleras ? bb : 0;

            double[] P(double t, double z) => new[] { a[0] + ux * t, a[1] + uy * t, z };
            void Horizontal(double t0, double t1, double z, string rol)
            {
                if (t1 - t0 > 0.02) piezas.Add(new PiezaDef { Rol = rol, Inicio = P(t0, z), Fin = P(t1, z), Ancho = bb, Profundidad = hh });
            }

            // vanos recortados al tramo útil
            var vanos = new List<VanoMuro>();
            foreach (var v in aberturas)
            {
                double t0 = Math.Max(s0 + bb, v.T0), t1 = Math.Min(s1 - bb, v.T1);
                if (t1 - t0 < 0.05) continue;
                double zs = Math.Min(zTope - sol - bb, v.ZSup);
                vanos.Add(new VanoMuro { T0 = t0, T1 = t1, ZInf = v.ZInf, ZSup = zs, Puerta = v.Puerta || v.ZInf - zBase < 0.01 });
            }

            double zb = zBase + sol, zt = zTope - sol;

            if (c.Soleras)
            {
                double t = s0;
                foreach (var v in vanos.Where(v => v.Puerta).OrderBy(v => v.T0))
                {
                    Horizontal(t, v.T0, zBase + bb / 2, "solera_inferior");
                    t = v.T1;
                }
                Horizontal(t, s1, zBase + bb / 2, "solera_inferior");
                Horizontal(s0, s1, zTope - bb / 2, "solera_superior");
            }

            var verticales = new List<Vertical>
            {
                new() { T = s0 + bb / 2, Z0 = zb, Z1 = zt, Rol = "pie_derecho" },
                new() { T = s1 - bb / 2, Z0 = zb, Z1 = zt, Rol = "pie_derecho" },
            };
            for (double t = s0 + c.SeparacionPies; t < s1 - bb / 2 - bb; t += c.SeparacionPies)
                verticales.Add(new Vertical { T = t, Z0 = zb, Z1 = zt, Rol = "pie_derecho" });

            if (c.RefuerzoVanos)
            {
                foreach (var v in vanos)
                {
                    var jambas = new[] { v.T0 - bb / 2, v.T1 + bb / 2 };
                    var nuevos = new List<Vertical>();
                    foreach (var x in verticales)
                    {
                        if (jambas.Any(j => Math.Abs(x.T - j) < bb - Tol)) continue;      // la reemplaza la jamba
                        if (v.T0 - Tol < x.T && x.T < v.T1 + Tol)
                        {
                            if (!v.Puerta) nuevos.Add(new Vertical { T = x.T, Z0 = zb, Z1 = v.ZInf - bb, Rol = "pie_derecho_corto" });
                            nuevos.Add(new Vertical { T = x.T, Z0 = v.ZSup + bb, Z1 = zt, Rol = "pie_derecho_corto" });
                            continue;
                        }
                        nuevos.Add(x);
                    }
                    nuevos.AddRange(jambas.Select(j => new Vertical { T = j, Z0 = zb, Z1 = zt, Rol = "jamba" }));
                    verticales = nuevos;
                    Horizontal(v.T0, v.T1, v.ZSup + bb / 2, "dintel");
                    if (!v.Puerta) Horizontal(v.T0, v.T1, v.ZInf - bb / 2, "alfeizar");
                }
            }

            verticales = verticales.Where(x => x.Z1 - x.Z0 > 0.02).OrderBy(x => x.T).ToList();
            foreach (var x in verticales)
                piezas.Add(new PiezaDef { Rol = x.Rol, Inicio = P(x.T, x.Z0), Fin = P(x.T, x.Z1), Ancho = bb, Profundidad = hh });

            // cadenetas entre pies derechos
            for (double zc = zBase + c.SeparacionHorizontales; zc + bb / 2 < zt - Tol; zc += c.SeparacionHorizontales)
            {
                if (zc - bb / 2 < zb - Tol) continue;
                var cruzan = verticales.Where(x => x.Z0 < zc - bb / 2 + Tol && x.Z1 > zc + bb / 2 - Tol).OrderBy(x => x.T).ToList();
                for (int i = 0; i + 1 < cruzan.Count; i++)
                {
                    double g0 = cruzan[i].T + bb / 2, g1 = cruzan[i + 1].T - bb / 2;
                    if (g1 - g0 < 0.02) continue;
                    double gm = (g0 + g1) / 2;
                    if (vanos.Any(v => v.T0 - bb <= gm && gm <= v.T1 + bb && v.ZInf - 2 * bb <= zc && zc <= v.ZSup + 2 * bb)) continue;
                    Horizontal(g0, g1, zc, "cadeneta");
                }
            }
            return piezas;
        }
    }

}

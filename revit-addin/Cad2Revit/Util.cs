using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using Autodesk.Revit.DB;

namespace Cad2Revit
{
    internal static class U
    {
        /// <summary>Metros -> unidades internas de Revit (pies).</summary>
        public static double M(double metros) => UnitUtils.ConvertToInternalUnits(metros, UnitTypeId.Meters);

        private static readonly char[] Prohibidos = { '\\', ':', '{', '}', '[', ']', '|', ';', '<', '>', '?', '`', '~' };

        /// <summary>Reemplaza los caracteres que Revit no acepta en nombres de elementos.</summary>
        public static string NombreSeguro(string nombre)
        {
            if (string.IsNullOrEmpty(nombre)) return nombre;
            var sb = new StringBuilder(nombre.Length);
            foreach (var c in nombre) sb.Append(Array.IndexOf(Prohibidos, c) >= 0 ? '-' : c);
            return sb.ToString().Trim();
        }

        public static XYZ P(double[] p, double z = 0) => new XYZ(M(p[0]), M(p[1]), z);

        public static CurveLoop Contorno(List<double[]> pts, double z)
        {
            var loop = new CurveLoop();
            for (int i = 0; i < pts.Count; i++)
            {
                var a = P(pts[i], z);
                var b = P(pts[(i + 1) % pts.Count], z);
                if (a.DistanceTo(b) > 1e-6) loop.Append(Line.CreateBound(a, b));
            }
            return loop;
        }

        public static IEnumerable<T> De<T>(Document doc) where T : Element =>
            new FilteredElementCollector(doc).OfClass(typeof(T)).Cast<T>();

        /// <summary>Intenta fijar un parámetro probando varios nombres (inglés / español).</summary>
        public static bool Fijar(Element e, double valorInterno, params string[] nombres)
        {
            foreach (var n in nombres.Where(x => !string.IsNullOrEmpty(x)))
            {
                var p = e.LookupParameter(n);
                if (p != null && !p.IsReadOnly && p.StorageType == StorageType.Double)
                {
                    p.Set(valorInterno);
                    return true;
                }
            }
            return false;
        }

        public static bool Fijar(Element e, BuiltInParameter bip, double valorInterno)
        {
            var p = e.get_Parameter(bip);
            if (p == null || p.IsReadOnly) return false;
            p.Set(valorInterno);
            return true;
        }

        public static bool Fijar(Element e, BuiltInParameter bip, ElementId valor)
        {
            var p = e.get_Parameter(bip);
            if (p == null || p.IsReadOnly) return false;
            p.Set(valor);
            return true;
        }
    }

    /// <summary>
    /// Silencia advertencias de Revit (las anota en el informe) y, si hay errores que
    /// Revit no puede resolver, los registra y deshace solo la transacción en curso.
    /// </summary>
    internal class ManejadorFallas : IFailuresPreprocessor
    {
        private readonly Informe _informe;
        public List<string> Errores { get; } = new();
        public ManejadorFallas(Informe informe) { _informe = informe; }

        public FailureProcessingResult PreprocessFailures(FailuresAccessor fa)
        {
            foreach (var f in fa.GetFailureMessages())
            {
                if (f.GetSeverity() == FailureSeverity.Warning)
                {
                    _informe.Advertencia("Revit: " + f.GetDescriptionText());
                    fa.DeleteWarning(f);
                }
                else
                {
                    Errores.Add(f.GetDescriptionText());
                }
            }
            return Errores.Count > 0 ? FailureProcessingResult.ProceedWithRollBack : FailureProcessingResult.Continue;
        }
    }

    public class Informe
    {
        public Dictionary<string, int> Creados { get; } = new();
        public List<string> Advertencias { get; } = new();
        public List<string> Errores { get; } = new();
        public SortedDictionary<string, List<string>> Familias { get; } = new();
        public List<string> Verificacion { get; } = new();

        public void Contar(string que) => Creados[que] = Creados.TryGetValue(que, out var n) ? n + 1 : 1;
        public void Advertencia(string s) => Advertencias.Add(s);
        public void Error(string s) => Errores.Add(s);

        public string Resumen()
        {
            var sb = new StringBuilder();
            foreach (var kv in Creados) sb.AppendLine($"{kv.Key}: {kv.Value}");
            sb.AppendLine($"Advertencias: {Advertencias.Count}   Errores: {Errores.Count}");
            return sb.ToString();
        }

        public string Detalle()
        {
            var sb = new StringBuilder();
            foreach (var e in Errores) sb.AppendLine("ERROR  " + e);
            foreach (var a in Advertencias) sb.AppendLine("aviso  " + a);
            if (Verificacion.Count > 0)
            {
                sb.AppendLine();
                sb.AppendLine("VERIFICACIÓN DE PUERTAS Y VENTANAS");
                foreach (var v in Verificacion) sb.AppendLine("  " + v);
            }
            if (Familias.Count > 0)
            {
                sb.AppendLine();
                sb.AppendLine("FAMILIAS CARGADAS EN LA PLANTILLA (familia : tipo) - usa estos nombres en los lineamientos");
                foreach (var kv in Familias)
                {
                    sb.AppendLine("  " + kv.Key + ":");
                    foreach (var f in kv.Value) sb.AppendLine("    " + f);
                }
            }
            return sb.ToString();
        }

        public void Guardar(string ruta)
        {
            try { File.WriteAllText(ruta, Resumen() + Environment.NewLine + Detalle()); }
            catch { /* carpeta de solo lectura: se ignora */ }
        }
    }
}

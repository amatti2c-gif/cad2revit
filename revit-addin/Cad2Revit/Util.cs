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
    /// Procesa las fallas al confirmar una etapa:
    ///  - advertencias: se anotan en el informe y se descartan;
    ///  - errores con elementos responsables: se eliminan SOLO esos elementos (lo
    ///    estándar en la API de Revit) y se informa cuál fue y por qué;
    ///  - errores con resolución propuesta por Revit: se aplica esa resolución;
    ///  - el resto: se deshace la etapa y se informa.
    /// </summary>
    internal class ManejadorFallas : IFailuresPreprocessor
    {
        private readonly Informe _informe;
        private readonly Dictionary<ElementId, string> _origen;
        private readonly Dictionary<ElementId, string> _eliminados;
        public List<string> NoResueltas { get; } = new();

        public ManejadorFallas(Informe informe, Dictionary<ElementId, string> origen, Dictionary<ElementId, string> eliminados)
        {
            _informe = informe;
            _origen = origen;
            _eliminados = eliminados;
        }

        public FailureProcessingResult PreprocessFailures(FailuresAccessor fa)
        {
            bool modificado = false;
            foreach (var f in fa.GetFailureMessages())
            {
                var texto = f.GetDescriptionText();
                if (f.GetSeverity() == FailureSeverity.Warning)
                {
                    _informe.Advertencia("Revit: " + texto);
                    fa.DeleteWarning(f);
                    continue;
                }
                // Solo se eliminan elementos "colgantes" (puertas, ventanas, columnas, vigas, zapatas,
                // vanos); nunca muros, losas o niveles, que sostienen al resto del modelo.
                var ids = f.GetFailingElementIds()
                           .Where(id => fa.GetDocument().GetElement(id) is FamilyInstance || fa.GetDocument().GetElement(id) is Opening)
                           .ToList();
                if (ids.Count > 0)
                {
                    foreach (var id in ids)
                    {
                        _eliminados[id] = texto;
                        var quien = _origen.TryGetValue(id, out var o) ? o : $"elemento {id}";
                        _informe.Advertencia($"Revit rechazó {quien} y se eliminó: {texto.Trim()}");
                    }
                    fa.DeleteElements(ids);
                    modificado = true;
                }
                else if (f.HasResolutions())
                {
                    fa.ResolveFailure(f);
                    modificado = true;
                }
                else
                {
                    NoResueltas.Add(texto.Trim());
                }
            }
            if (NoResueltas.Count > 0) return FailureProcessingResult.ProceedWithRollBack;
            return modificado ? FailureProcessingResult.ProceedWithCommit : FailureProcessingResult.Continue;
        }
    }

    public class Informe
    {
        public Dictionary<string, int> Creados { get; } = new();
        public List<string> Advertencias { get; } = new();
        public List<string> Errores { get; } = new();
        public SortedDictionary<string, List<string>> Familias { get; } = new();
        public List<string> Verificacion { get; } = new();

        // familia|tipo|categoría -> (motivo, elementos afectados)
        private readonly SortedDictionary<string, (string motivo, List<string> ids)> _faltantes = new();

        /// <summary>Registra un elemento que no se pudo crear con la familia estándar indicada.</summary>
        public void Faltante(string familia, string tipo, string categoria, string id, string motivo)
        {
            var clave = $"{familia}|{tipo}|{categoria}";
            if (!_faltantes.TryGetValue(clave, out var v)) _faltantes[clave] = v = (motivo, new List<string>());
            v.ids.Add(id);
        }

        public int CantidadFaltantes => _faltantes.Values.Sum(v => v.ids.Count);

        public void Contar(string que, int cuantos = 1) => Creados[que] = (Creados.TryGetValue(que, out var n) ? n : 0) + cuantos;
        public void Advertencia(string s) => Advertencias.Add(s);
        public void Error(string s) => Errores.Add(s);

        public string Resumen()
        {
            var sb = new StringBuilder();
            foreach (var kv in Creados) sb.AppendLine($"{kv.Key}: {kv.Value}");
            sb.AppendLine($"Advertencias: {Advertencias.Count}   Errores: {Errores.Count}   " +
                          $"No creados por familia faltante: {CantidadFaltantes}");
            return sb.ToString();
        }

        public string Detalle()
        {
            var sb = new StringBuilder();
            if (_faltantes.Count > 0)
            {
                sb.AppendLine("NO SE PUDO CREAR CON FAMILIAS ESTÁNDAR DE REVIT");
                foreach (var kv in _faltantes)
                {
                    var p = kv.Key.Split('|');
                    sb.AppendLine($"  - Familia '{p[0]}'" + (string.IsNullOrEmpty(p[1]) ? "" : $", tipo '{p[1]}'") +
                                  $" ({p[2]}): {kv.Value.ids.Count} elemento(s) sin crear");
                    sb.AppendLine("      " + (kv.Value.motivo ?? "no está en el proyecto ni en la biblioteca de Revit de este equipo"));
                    sb.AppendLine("      Elementos: " + string.Join(", ", kv.Value.ids.Take(12)) + (kv.Value.ids.Count > 12 ? ", …" : ""));
                }
                sb.AppendLine("  Para resolverlo una sola vez: carga la familia en tu PLANTILLA (Insertar → Cargar familia de");
                sb.AppendLine("  Autodesk) o copia el .rfa a Documentos\\cad2revit\\Familias; desde ahí se carga sola.");
                sb.AppendLine();
            }
            foreach (var e in Errores) sb.AppendLine("ERROR  " + e);
            // avisos idénticos se muestran una vez con su cantidad
            foreach (var g in Advertencias.GroupBy(a => a))
                sb.AppendLine("aviso  " + (g.Count() > 1 ? $"(x{g.Count()}) " : "") + g.Key);
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

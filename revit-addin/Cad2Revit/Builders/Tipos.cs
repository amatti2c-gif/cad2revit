using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;

namespace Cad2Revit
{
    public partial class Constructor
    {
        private static MaterialFunctionAssignment Funcion(string f)
        {
            switch (f)
            {
                case "estructura": return MaterialFunctionAssignment.Structure;
                case "sustrato": return MaterialFunctionAssignment.Substrate;
                case "aislacion": return MaterialFunctionAssignment.Insulation;
                case "terminacion1": return MaterialFunctionAssignment.Finish1;
                case "terminacion2": return MaterialFunctionAssignment.Finish2;
                case "membrana": return MaterialFunctionAssignment.Membrane;
                default: return MaterialFunctionAssignment.Structure;
            }
        }

        private void CrearMaterialesYTipos()
        {
            // Materiales
            var existentes = U.De<Material>(_doc).GroupBy(m => m.Name).ToDictionary(g => g.Key, g => g.First());
            // Las claves de los diccionarios internos usan el nombre del JSON; en Revit se usa
            // U.NombreSeguro (Revit prohíbe caracteres como [ ] { } : ; | < > ? ` ~ \).
            foreach (var md in _m.Materiales)
            {
                Intentar("Material " + md.Nombre, () =>
                {
                    if (!existentes.TryGetValue(U.NombreSeguro(md.Nombre), out var mat))
                    {
                        mat = (Material)_doc.GetElement(Material.Create(_doc, U.NombreSeguro(md.Nombre)));
                        _inf.Contar("Materiales");
                    }
                    if (md.Color != null && md.Color.Length == 3)
                        mat.Color = new Color((byte)md.Color[0], (byte)md.Color[1], (byte)md.Color[2]);
                    if (!string.IsNullOrEmpty(md.Clase)) mat.MaterialClass = md.Clase;
                    _materiales[md.Nombre] = mat.Id;
                });
            }

            // Tipos de muro
            // Base: un muro básico genérico (sin configuraciones especiales heredadas)
            var baseMuro = U.De<WallType>(_doc).Where(w => w.Kind == WallKind.Basic && w.GetCompoundStructure() != null)
                               .OrderByDescending(w => w.Name.IndexOf("Generic", System.StringComparison.OrdinalIgnoreCase) >= 0
                                                    || w.Name.IndexOf("Genérico", System.StringComparison.OrdinalIgnoreCase) >= 0)
                               .ThenBy(w => w.GetCompoundStructure().LayerCount)
                               .FirstOrDefault();
            if (baseMuro == null) { _inf.Error("La plantilla no tiene ningún tipo de muro básico"); return; }
            var murosExist = U.De<WallType>(_doc).GroupBy(w => w.Name).ToDictionary(g => g.Key, g => g.First());
            foreach (var t in _m.TiposMuro)
            {
                Intentar("Tipo de muro " + t.Nombre, () =>
                {
                    if (!murosExist.TryGetValue(U.NombreSeguro(t.Nombre), out var wt))
                    {
                        wt = (WallType)baseMuro.Duplicate(U.NombreSeguro(t.Nombre));
                        _inf.Contar("Tipos de muro");
                    }
                    var csMuro = Estructura(t, wt.GetCompoundStructure() ?? baseMuro.GetCompoundStructure());
                    // Sin envolver capas en puertas/ventanas: en muros delgados el envolvente deja sin
                    // espacio al marco de la puerta ("Profile sketch is empty").
                    csMuro.OpeningWrapping = OpeningWrappingCondition.None;
                    wt.SetCompoundStructure(csMuro);
                    _tiposMuro[t.Nombre] = wt;
                });
            }

            // Tipos de losa / cubierta
            // Base: un tipo de suelo con estructura de capas (no losa de cimentación)
            var baseLosa = U.De<FloorType>(_doc).Where(f => !f.IsFoundationSlab && f.GetCompoundStructure() != null)
                               .OrderByDescending(f => f.Name.IndexOf("Generic", System.StringComparison.OrdinalIgnoreCase) >= 0
                                                    || f.Name.IndexOf("Genérico", System.StringComparison.OrdinalIgnoreCase) >= 0)
                               .FirstOrDefault();
            if (baseLosa == null) { _inf.Error("La plantilla no tiene ningún tipo de suelo"); return; }
            var losasExist = U.De<FloorType>(_doc).GroupBy(f => f.Name).ToDictionary(g => g.Key, g => g.First());
            foreach (var t in _m.TiposLosa)
            {
                Intentar("Tipo de losa " + t.Nombre, () =>
                {
                    if (!losasExist.TryGetValue(U.NombreSeguro(t.Nombre), out var ft))
                    {
                        ft = (FloorType)baseLosa.Duplicate(U.NombreSeguro(t.Nombre));
                        _inf.Contar("Tipos de losa");
                    }
                    ft.SetCompoundStructure(Estructura(t, ft.GetCompoundStructure() ?? baseLosa.GetCompoundStructure()));
                    _tiposLosa[t.Nombre] = ft;
                });
            }
        }

        /// <summary>
        /// Capas en el orden del JSON: muros de exterior a interior, losas de
        /// arriba hacia abajo (coincide con el índice 0 de Revit). Las capas antes
        /// de la primera "estructura" son la cáscara exterior; las posteriores a la
        /// última, la interior.
        /// Se parte de la estructura del tipo base (no de una nueva) porque Revit exige
        /// que cada categoría conserve sus condiciones de borde (EndCap, envolventes);
        /// una estructura creada desde cero es rechazada por los tipos de suelo.
        /// </summary>
        private CompoundStructure Estructura(TipoDef t, CompoundStructure plantilla)
        {
            var capas = new List<CompoundStructureLayer>();
            foreach (var c in t.Capas)
            {
                var ancho = c.Funcion == "membrana" ? 0.0 : U.M(c.Espesor);  // Revit exige membranas de espesor 0
                capas.Add(new CompoundStructureLayer(ancho, Funcion(c.Funcion), IdMaterial(c.Material)));
            }
            CompoundStructure cs;
            if (plantilla != null)
            {
                cs = plantilla;
                cs.SetLayers(capas);
            }
            else
            {
                cs = CompoundStructure.CreateSimpleCompoundStructure(capas);
            }
            int primera = t.Capas.FindIndex(c => c.Funcion == "estructura");
            int ultima = t.Capas.FindLastIndex(c => c.Funcion == "estructura");
            if (primera >= 0)
            {
                cs.SetNumberOfShellLayers(ShellLayerType.Exterior, primera);
                cs.SetNumberOfShellLayers(ShellLayerType.Interior, t.Capas.Count - 1 - ultima);
                cs.StructuralMaterialIndex = primera;
            }
            if (!cs.IsValid(_doc, out var errores, out _))
                _inf.Advertencia($"Tipo '{t.Nombre}': estructura de capas con problemas ({string.Join(", ", errores.Values)})");
            return cs;
        }

        /// <summary>
        /// Devuelve un tipo de familia con las medidas pedidas: reutiliza el
        /// existente si ya cumple, si no lo duplica como "Tipo - sufijo".
        /// </summary>
        private FamilySymbol ConMedidas(FamilySymbol s, string sufijo,
            params (BuiltInParameter? bip, string[] nombres, double metros)[] medidas)
        {
            Parameter Param(FamilySymbol x, (BuiltInParameter? bip, string[] nombres, double metros) md)
            {
                var p = md.bip.HasValue ? x.get_Parameter(md.bip.Value) : null;
                return p ?? md.nombres.Select(x.LookupParameter).FirstOrDefault(q => q != null && q.StorageType == StorageType.Double);
            }
            double Distancia(FamilySymbol x) => medidas.Sum(md =>
            {
                var p = Param(x, md);
                return p == null || p.StorageType != StorageType.Double ? 1e9 : System.Math.Abs(p.AsDouble() - U.M(md.metros));
            });

            // 1) un tipo de la misma familia que ya tenga esas medidas
            var hermanos = s.Family.GetFamilySymbolIds().Select(id => (FamilySymbol)_doc.GetElement(id)).ToList();
            var exacto = hermanos.Where(x => Distancia(x) < U.M(0.001) * medidas.Length)
                                 .OrderBy(x => x.Id == s.Id ? 0 : 1).FirstOrDefault();
            if (exacto != null) return Activar(exacto);

            // 2) un duplicado creado antes por cad2revit
            var nombre = U.NombreSeguro($"{s.Name} - {sufijo}");
            var cercano = hermanos.OrderBy(Distancia).First();
            nombre = U.NombreSeguro($"{cercano.Name} - {sufijo}");
            var existente = hermanos.FirstOrDefault(x => x.Name == nombre);
            if (existente != null) return Activar(existente);

            // 3) duplicar el tipo de medidas más cercanas (cambia menos la geometría de la familia)
            var nuevo = (FamilySymbol)cercano.Duplicate(nombre);
            foreach (var md in medidas)
            {
                bool ok = md.bip.HasValue && U.Fijar(nuevo, md.bip.Value, U.M(md.metros));
                ok = ok || U.Fijar(nuevo, U.M(md.metros), md.nombres);
                if (!ok)
                    _inf.Advertencia($"Familia '{s.FamilyName}': no se encontró parámetro editable " +
                                     $"{string.Join("/", md.nombres)}; revisa los nombres en estructuracion.yaml");
            }
            // marca de tipo única para evitar el aviso de "Type Mark" duplicado
            var marca = nuevo.get_Parameter(BuiltInParameter.ALL_MODEL_TYPE_MARK);
            if (marca != null && !marca.IsReadOnly) marca.Set(sufijo);
            return Activar(nuevo);
        }

        private FamilySymbol Activar(FamilySymbol s)
        {
            if (!s.IsActive) { s.Activate(); _doc.Regenerate(); }
            return s;
        }

        // ---------------------------------------------------------------- familias estándar
        // Regla única: cada elemento se crea con la familia y el tipo que indica la app. Si la
        // familia no está en el proyecto se carga automáticamente desde la biblioteca de Revit.
        // Si tampoco está ahí, el elemento NO se crea y queda en la sección "faltantes" del informe.

        private static string SinPrefijo(string n) => (n ?? "").StartsWith("M_") ? n.Substring(2) : (n ?? "");

        private FamilySymbol BuscarSimbolo(BuiltInCategory cat, string familia, string tipo)
        {
            var l = new FilteredElementCollector(_doc).OfClass(typeof(FamilySymbol)).OfCategory(cat)
                        .Cast<FamilySymbol>().ToList();
            bool MismaFamilia(FamilySymbol x) => x.FamilyName == familia ||
                string.Equals(SinPrefijo(x.FamilyName), SinPrefijo(familia), System.StringComparison.OrdinalIgnoreCase);
            return l.FirstOrDefault(x => MismaFamilia(x) && (tipo == null || x.Name == tipo))
                   ?? l.FirstOrDefault(MismaFamilia);
        }

        private FamilySymbol Simbolo(BuiltInCategory cat, string familia, string tipo, string id)
        {
            if (string.IsNullOrEmpty(familia))
            {
                _inf.Faltante("(sin familia)", tipo, cat.ToString(), id, "los lineamientos no indican qué familia usar");
                return null;
            }
            var s = BuscarSimbolo(cat, familia, tipo);
            if (s == null && CargarDesdeBiblioteca(familia)) s = BuscarSimbolo(cat, familia, tipo);
            if (s == null)
            {
                _inf.Faltante(familia, tipo, cat.ToString(), id, null);
                return null;
            }
            return Activar(s);
        }

        /// <summary>
        /// Para piezas de sección rectangular (pilares, vigas, entramado): si la familia indicada no
        /// existe, se usa cualquier familia estándar de la categoría cuya sección sea editable
        /// (prefiriendo las que calzan con el material) y luego se ajustan sus dimensiones.
        /// </summary>
        private FamilySymbol SimboloRectangular(BuiltInCategory cat, string familia, string tipo, string id,
                                                string[] nombresB, string[] nombresH, params string[] preferidas)
        {
            var s = string.IsNullOrEmpty(familia) ? null : BuscarSimbolo(cat, familia, tipo);
            if (s == null && !string.IsNullOrEmpty(familia) && CargarDesdeBiblioteca(familia)) s = BuscarSimbolo(cat, familia, tipo);
            if (s != null) return Activar(s);

            bool Editable(FamilySymbol x, string[] nombres) => nombres.Any(n =>
            {
                var p = x.LookupParameter(n);
                return p != null && !p.IsReadOnly && p.StorageType == StorageType.Double;
            });
            var candidatos = new FilteredElementCollector(_doc).OfClass(typeof(FamilySymbol)).OfCategory(cat)
                .Cast<FamilySymbol>()
                .Where(x => !NoRectangulares.Any(k => (x.FamilyName + " " + x.Name).ToLowerInvariant().Contains(k)))
                .Where(x => Editable(x, nombresB) && Editable(x, nombresH)).ToList();
            int Preferencia(FamilySymbol x)
            {
                var n = (x.FamilyName + " " + x.Name).ToLowerInvariant();
                return preferidas.Any(k => n.Contains(k)) ? 0 : n.Contains("rectangular") ? 1 : 2;
            }
            var elegido = candidatos.OrderBy(Preferencia).ThenBy(x => x.FamilyName).FirstOrDefault();
            if (elegido == null)
            {
                _inf.Faltante(familia, tipo, cat.ToString(), id,
                              "no está disponible y no hay ninguna familia de esa categoría con sección editable");
                return null;
            }
            var clave = $"{cat}|{familia}";
            if (_avisosRectangular.Add(clave))
                _inf.Advertencia($"'{familia}' no está disponible; se usa '{elegido.FamilyName}' con sus dimensiones " +
                                 $"ajustadas (primer caso: {id}).");
            return Activar(elegido);
        }

        private readonly HashSet<string> _avisosRectangular = new();

        // Perfiles cuya forma no es un rectángulo macizo: nunca se usan como sustituto
        private static readonly string[] NoRectangulares =
            { "shapes", "hss", "pipe", "steel", "acero", "uc-", "ub-", "universal", "reference", "referencia", "channel", "angle", "joist", "truss", "pile" };

        private readonly Dictionary<string, bool> _intentosCarga = new();

        /// <summary>Carpetas donde se buscan familias estándar (.rfa) para cargarlas solas.</summary>
        private IEnumerable<string> CarpetasFamilias()
        {
            var version = _doc.Application.VersionNumber;
            var comun = System.Environment.GetFolderPath(System.Environment.SpecialFolder.CommonApplicationData);
            var docs = System.Environment.GetFolderPath(System.Environment.SpecialFolder.MyDocuments);
            var addin = System.IO.Path.GetDirectoryName(typeof(Constructor).Assembly.Location) ?? "";
            return new[]
            {
                System.IO.Path.Combine(comun, "Autodesk", $"RVT {version}", "Libraries"),   // biblioteca de Revit
                System.IO.Path.Combine(docs, "cad2revit", "Familias"),                      // carpeta propia
                System.IO.Path.Combine(addin, "Familias"),
            }.Where(System.IO.Directory.Exists);
        }

        private bool CargarDesdeBiblioteca(string familia)
        {
            if (_intentosCarga.TryGetValue(familia, out var ya)) return ya;
            var nombres = new[] { familia, SinPrefijo(familia), "M_" + SinPrefijo(familia) }
                          .Distinct().Select(n => n + ".rfa").ToList();
            foreach (var raiz in CarpetasFamilias())
            {
                foreach (var nombre in nombres)
                {
                    string archivo = null;
                    try
                    {
                        archivo = System.IO.Directory.EnumerateFiles(raiz, nombre, System.IO.SearchOption.AllDirectories)
                                                     .FirstOrDefault();
                    }
                    catch { /* carpetas sin permiso */ }
                    if (archivo != null && _doc.LoadFamily(archivo, out _))
                    {
                        _inf.Advertencia($"Familia '{familia}' cargada automáticamente desde {archivo}");
                        return _intentosCarga[familia] = true;
                    }
                }
            }
            return _intentosCarga[familia] = false;
        }
    }
}

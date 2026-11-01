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
            var baseMuro = U.De<WallType>(_doc).FirstOrDefault(w => w.Kind == WallKind.Basic);
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
                    wt.SetCompoundStructure(Estructura(t, wt.GetCompoundStructure() ?? baseMuro.GetCompoundStructure()));
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

        // Palabras que hacen preferible una familia cuando la pedida no está cargada
        private static readonly Dictionary<BuiltInCategory, (string[] mas, string[] menos)> Afinidad = new()
        {
            [BuiltInCategory.OST_Doors] = (new[] { "single", "simple", "flush", "interior", "1 hoja" },
                                           new[] { "double", "doble", "garage", "curtain", "bifold", "sliding" }),
            [BuiltInCategory.OST_Windows] = (new[] { "fixed", "fija", "fijo" }, new[] { "skylight", "curtain" }),
            [BuiltInCategory.OST_StructuralColumns] = (new[] { "concrete", "hormig", "rectangular" },
                                                       new[] { "steel", "acero", "hss", "pipe", "wide flange", "uc-", "universal", "reference", "referencia" }),
            [BuiltInCategory.OST_StructuralFraming] = (new[] { "concrete", "hormig", "rectangular" },
                                                       new[] { "reference", "referencia", "steel", "acero", "joist", "truss", "shapes", "hss" }),
            [BuiltInCategory.OST_StructuralFoundation] = (new[] { "footing", "zapata", "rectangular" }, new[] { "pile", "pilote" }),
        };

        private readonly HashSet<string> _avisosFamilia = new();

        // En estructura no se reemplaza por una familia de otro material (p.ej. acero en vez de hormigón)
        private static readonly HashSet<BuiltInCategory> SoloAfines = new()
        {
            BuiltInCategory.OST_StructuralColumns, BuiltInCategory.OST_StructuralFraming, BuiltInCategory.OST_StructuralFoundation,
        };

        private FamilySymbol Simbolo(BuiltInCategory cat, string familia, string tipo, string id)
        {
            List<FamilySymbol> Simbolos() => new FilteredElementCollector(_doc).OfClass(typeof(FamilySymbol)).OfCategory(cat)
                                                 .Cast<FamilySymbol>().ToList();
            var simbolos = Simbolos();
            FamilySymbol Buscar(List<FamilySymbol> l) =>
                l.FirstOrDefault(x => x.FamilyName == familia && (tipo == null || x.Name == tipo))
                ?? l.FirstOrDefault(x => x.FamilyName == familia);

            var s = Buscar(simbolos);
            if (s == null && !string.IsNullOrEmpty(familia) && CargarDesdeBiblioteca(familia))
                s = Buscar(simbolos = Simbolos());

            if (s == null && simbolos.Count > 0)
            {
                int Puntaje(FamilySymbol x)
                {
                    if (!Afinidad.TryGetValue(cat, out var af)) return 0;
                    var n = (x.FamilyName + " " + x.Name).ToLowerInvariant();
                    return af.mas.Count(n.Contains) * 2 - af.menos.Count(n.Contains) * 5;
                }
                var mejor = simbolos.OrderByDescending(Puntaje).ThenBy(x => x.FamilyName).First();
                if (SoloAfines.Contains(cat) && Puntaje(mejor) <= 0)
                {
                    if (_avisosFamilia.Add($"{cat}|{familia}"))
                        _inf.Error($"Familia '{familia}' no está cargada ni se encontró en la biblioteca de Revit, y no hay " +
                                   "una equivalente en la plantilla; no se crearon esos elementos. Cárgala con Insertar → " +
                                   "Cargar familia (o desde Autodesk Content) y vuelve a construir.");
                    return null;
                }
                s = mejor;
                if (_avisosFamilia.Add($"{cat}|{familia}"))
                    _inf.Advertencia($"Familia '{familia}' no está cargada en la plantilla; se usa '{s.FamilyName} : {s.Name}' " +
                                     $"(primer caso: {id}). Cárgala (Insertar → Cargar familia) o cambia el nombre en los lineamientos.");
            }
            return s == null ? null : Activar(s);
        }

        private readonly HashSet<string> _intentosCarga = new();

        /// <summary>Busca "familia.rfa" en las bibliotecas locales de Revit y la carga en el proyecto.</summary>
        private bool CargarDesdeBiblioteca(string familia)
        {
            if (!_intentosCarga.Add(familia)) return false;
            var version = _doc.Application.VersionNumber;
            var raices = new[]
            {
                System.IO.Path.Combine(System.Environment.GetFolderPath(System.Environment.SpecialFolder.CommonApplicationData),
                                       "Autodesk", $"RVT {version}", "Libraries"),
                System.IO.Path.Combine(System.Environment.GetFolderPath(System.Environment.SpecialFolder.CommonApplicationData),
                                       "Autodesk", $"RVT {version}"),
            };
            foreach (var raiz in raices.Where(System.IO.Directory.Exists))
            {
                string archivo = null;
                try
                {
                    archivo = System.IO.Directory.EnumerateFiles(raiz, familia + ".rfa", System.IO.SearchOption.AllDirectories)
                                                 .FirstOrDefault();
                }
                catch { /* carpetas sin permiso */ }
                if (archivo != null && _doc.LoadFamily(archivo, out _))
                {
                    _inf.Advertencia($"Familia '{familia}' cargada automáticamente desde {archivo}");
                    return true;
                }
            }
            return false;
        }
    }
}

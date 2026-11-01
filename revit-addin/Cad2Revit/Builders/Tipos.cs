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
            bool Cumple(FamilySymbol x) => medidas.All(md =>
            {
                var p = md.bip.HasValue ? x.get_Parameter(md.bip.Value) : null;
                p ??= md.nombres.Select(x.LookupParameter).FirstOrDefault(q => q != null);
                return p != null && p.StorageType == StorageType.Double && System.Math.Abs(p.AsDouble() - U.M(md.metros)) < U.M(0.001);
            });
            if (Cumple(s)) return s;

            var nombre = U.NombreSeguro($"{s.Name} - {sufijo}");
            var existente = s.Family.GetFamilySymbolIds().Select(id => (FamilySymbol)_doc.GetElement(id))
                             .FirstOrDefault(x => x.Name == nombre);
            if (existente != null) return existente;

            var nuevo = (FamilySymbol)s.Duplicate(nombre);
            foreach (var md in medidas)
            {
                bool ok = md.bip.HasValue && U.Fijar(nuevo, md.bip.Value, U.M(md.metros));
                ok = ok || U.Fijar(nuevo, U.M(md.metros), md.nombres);
                if (!ok)
                    _inf.Advertencia($"Familia '{s.FamilyName}': no se encontró parámetro editable " +
                                     $"{string.Join("/", md.nombres)}; revisa los nombres en estructuracion.yaml");
            }
            return nuevo;
        }

        // Palabras que hacen preferible una familia cuando la pedida no está cargada
        private static readonly Dictionary<BuiltInCategory, (string[] mas, string[] menos)> Afinidad = new()
        {
            [BuiltInCategory.OST_Doors] = (new[] { "single", "simple", "flush", "interior", "1 hoja" },
                                           new[] { "double", "doble", "garage", "curtain", "bifold", "sliding" }),
            [BuiltInCategory.OST_Windows] = (new[] { "fixed", "fija", "fijo" }, new[] { "skylight", "curtain" }),
            [BuiltInCategory.OST_StructuralColumns] = (new[] { "concrete", "hormig", "rectangular" },
                                                       new[] { "steel", "acero", "hss", "pipe", "wide flange", "uc-", "w-" }),
            [BuiltInCategory.OST_StructuralFraming] = (new[] { "concrete", "hormig", "rectangular" },
                                                       new[] { "reference", "referencia", "steel", "acero", "joist", "truss" }),
            [BuiltInCategory.OST_StructuralFoundation] = (new[] { "footing", "zapata", "rectangular" }, new[] { "pile", "pilote" }),
        };

        private readonly HashSet<string> _avisosFamilia = new();

        private FamilySymbol Simbolo(BuiltInCategory cat, string familia, string tipo, string id)
        {
            var simbolos = new FilteredElementCollector(_doc).OfClass(typeof(FamilySymbol)).OfCategory(cat)
                               .Cast<FamilySymbol>().ToList();
            var s = simbolos.FirstOrDefault(x => x.FamilyName == familia && (tipo == null || x.Name == tipo))
                    ?? simbolos.FirstOrDefault(x => x.FamilyName == familia);
            if (s == null && simbolos.Count > 0)
            {
                int Puntaje(FamilySymbol x)
                {
                    if (!Afinidad.TryGetValue(cat, out var af)) return 0;
                    var n = (x.FamilyName + " " + x.Name).ToLowerInvariant();
                    return af.mas.Count(n.Contains) * 2 - af.menos.Count(n.Contains) * 5;
                }
                s = simbolos.OrderByDescending(Puntaje).ThenBy(x => x.FamilyName).First();
                if (_avisosFamilia.Add($"{cat}|{familia}"))
                    _inf.Advertencia($"Familia '{familia}' no está cargada en la plantilla; se usa '{s.FamilyName} : {s.Name}' " +
                                     $"(primer caso: {id}). Cárgala (Insertar → Cargar familia) o cambia el nombre en los lineamientos.");
            }
            if (s != null && !s.IsActive)
            {
                s.Activate();
                _doc.Regenerate();
            }
            return s;
        }
    }
}

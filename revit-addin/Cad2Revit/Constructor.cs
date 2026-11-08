using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;

namespace Cad2Revit
{
    /// <summary>
    /// Construye el modelo en Revit a partir del JSON. No toma decisiones de
    /// diseño: todo (tipos, LOD, qué se crea) ya viene resuelto por el parser.
    /// Cada etapa corre en su propia transacción dentro de un TransactionGroup,
    /// de modo que se puede deshacer todo con un solo Ctrl+Z.
    /// </summary>
    public partial class Constructor
    {
        private readonly Document _doc;
        private readonly Modelo _m;
        private readonly Informe _inf = new();

        private readonly Dictionary<string, Level> _niveles = new();
        private readonly Dictionary<string, ElementId> _materiales = new();
        private readonly Dictionary<string, WallType> _tiposMuro = new();
        private readonly Dictionary<string, FloorType> _tiposLosa = new();
        private readonly Dictionary<string, Wall> _muros = new();
        private readonly Dictionary<string, FamilyInstance> _columnas = new();

        /// <summary>Prefijo del parámetro Comentarios en todo lo que crea cad2revit.</summary>
        public const string Marca = "cad2revit ";

        /// <summary>Elementos creados por una importación anterior (identificados por su comentario).</summary>
        public static List<ElementId> ElementosPrevios(Document doc) =>
            new FilteredElementCollector(doc).WhereElementIsNotElementType()
                .Where(e => e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.AsString()?.StartsWith(Marca) == true)
                .Select(e => e.Id).ToList();

        /// <summary>Si es true, se borra la importación anterior antes de construir.</summary>
        public bool ReemplazarAnterior { get; set; }

        public Constructor(Document doc, Modelo modelo)
        {
            _doc = doc;
            _m = modelo;
        }

        public Informe Construir()
        {
            foreach (var a in _m.Advertencias) _inf.Advertencia("parser: " + a);

            using (var grupo = new TransactionGroup(_doc, "cad2revit: construir modelo"))
            {
                grupo.Start();
                if (ReemplazarAnterior) Etapa("Eliminar importación anterior", EliminarAnterior);
                Etapa("Materiales y tipos", CrearMaterialesYTipos);
                Etapa("Niveles y ejes", CrearNivelesYEjes);
                Etapa("Muros", CrearMuros);
                if (_m.Lod?.Aberturas != "ninguna") Etapa("Aberturas", CrearAberturas);
                Etapa("Losas y cielos", CrearLosasYCielos);
                Etapa("Entramado de madera", CrearEntramados);
                Etapa("Columnas", CrearColumnas);
                Etapa("Vigas", CrearVigas);
                Etapa("Fundaciones", CrearFundaciones);
                Etapa("Marcos de acero", CrearMarcos);
                if (_m.Recintos.Count > 0) Etapa("Recintos", CrearRecintos);
                VerificarAberturas();   // maneja sus propias etapas (reintentos confirmados uno a uno)
                grupo.Assimilate();
            }
            ListarFamilias();
            return _inf;
        }

        private string _etapa = "";
        private string _ultimoFallo = "";

        // ElementId de Revit -> id cad2revit (para traducir los rechazos de Revit al informe)
        private readonly Dictionary<ElementId, string> _origen = new();
        // Elementos que Revit eliminó al confirmar una etapa, con el motivo
        private readonly Dictionary<ElementId, string> _eliminadosPorRevit = new();

        /// <summary>Pone el comentario "cad2revit id" y recuerda el origen del elemento.</summary>
        private void Marcar(Element e, string id)
        {
            if (e == null) return;
            e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.Set(Marca + id);
            _origen[e.Id] = id;
        }

        /// <summary>
        /// Una transacción por etapa. Si Revit rechaza algún elemento al confirmar, el
        /// manejador de fallas elimina SOLO ese elemento (en vez de deshacer la etapa) y lo
        /// informa con su id de cad2revit.
        /// </summary>
        private void Etapa(string nombre, Action accion)
        {
            _etapa = nombre;
            var fallas = new ManejadorFallas(_inf, _origen, _eliminadosPorRevit);
            using (var t = new Transaction(_doc, "cad2revit: " + nombre))
            {
                var op = t.GetFailureHandlingOptions();
                op.SetFailuresPreprocessor(fallas);
                op.SetClearAfterRollback(true);
                t.SetFailureHandlingOptions(op);
                if (t.Start() != TransactionStatus.Started)
                {
                    _inf.Error($"Etapa '{nombre}': Revit no permitió iniciar la transacción");
                    return;
                }
                try
                {
                    accion();
                    var estado = t.Commit();
                    if (estado != TransactionStatus.Committed)
                        _inf.Error($"Etapa '{nombre}': Revit deshizo la etapa completa ({estado})" +
                                   (fallas.NoResueltas.Count > 0 ? ": " + string.Join(" | ", fallas.NoResueltas) : ""));
                }
                catch (Exception ex)
                {
                    if (t.HasStarted() && !t.HasEnded()) t.RollBack();
                    _inf.Error($"Etapa '{nombre}' abortada: {ex.Message}");
                }
            }
        }

        /// <summary>Crea un elemento dentro de la etapa; una excepción afecta solo a ese elemento.</summary>
        private bool Intentar(string id, Action accion, bool registrarError = true)
        {
            using (var st = new SubTransaction(_doc))
            {
                var conteo = new Dictionary<string, int>(_inf.Creados);
                st.Start();
                try
                {
                    accion();
                    st.Commit();
                    return true;
                }
                catch (Exception ex)
                {
                    if (st.HasStarted() && !st.HasEnded()) st.RollBack();
                    _inf.Creados.Clear();
                    foreach (var kv in conteo) _inf.Creados[kv.Key] = kv.Value;
                    _ultimoFallo = ex.Message;
                    if (registrarError) _inf.Error($"{id}: {ex.Message}");
                    return false;
                }
            }
        }

        private void EliminarAnterior() => Intentar("eliminar importación anterior", () =>
        {
            var ids = ElementosPrevios(_doc);
            // primero lo alojado (puertas/ventanas), luego el resto; los ya borrados en cascada se ignoran
            int n = 0;
            foreach (var id in ids)
            {
                if (_doc.GetElement(id) == null) continue;
                try { _doc.Delete(id); n++; } catch { /* ya eliminado junto a su anfitrión */ }
            }
            _inf.Advertencia($"Se eliminaron {n} elementos de la importación anterior");
        });

        private void ListarFamilias()
        {
            var cats = new (BuiltInCategory cat, string nombre)[]
            {
                (BuiltInCategory.OST_Doors, "Puertas"), (BuiltInCategory.OST_Windows, "Ventanas"),
                (BuiltInCategory.OST_StructuralColumns, "Columnas estructurales"),
                (BuiltInCategory.OST_StructuralFraming, "Vigas"),
                (BuiltInCategory.OST_StructuralFoundation, "Fundaciones"),
            };
            foreach (var (cat, nombre) in cats)
            {
                _inf.Familias[nombre] = new FilteredElementCollector(_doc).OfClass(typeof(FamilySymbol)).OfCategory(cat)
                    .Cast<FamilySymbol>().Select(x => $"{x.FamilyName} : {x.Name}").Distinct().OrderBy(x => x).ToList();
            }
        }

        private Level Nivel(string nombre) =>
            nombre != null && _niveles.TryGetValue(nombre, out var n) ? n : null;

        private ElementId IdMaterial(string nombre) =>
            nombre != null && _materiales.TryGetValue(nombre, out var id) ? id : ElementId.InvalidElementId;

        // ------------------------------------------------------------ niveles
        private void CrearNivelesYEjes()
        {
            var existentes = U.De<Level>(_doc).GroupBy(l => l.Name).ToDictionary(g => g.Key, g => g.First());
            var tipoPlanta = U.De<ViewFamilyType>(_doc).FirstOrDefault(v => v.ViewFamily == ViewFamily.FloorPlan);
            var conVista = new HashSet<ElementId>(U.De<ViewPlan>(_doc).Where(v => !v.IsTemplate && v.GenLevel != null)
                                                                       .Select(v => v.GenLevel.Id));
            foreach (var n in _m.Niveles)
            {
                Intentar("Nivel " + n.Nombre, () =>
                {
                    if (!existentes.TryGetValue(U.NombreSeguro(n.Nombre), out var nivel))
                    {
                        nivel = Level.Create(_doc, U.M(n.Cota));
                        nivel.Name = U.NombreSeguro(n.Nombre);
                        _inf.Contar("Niveles");
                    }
                    else
                    {
                        nivel.Elevation = U.M(n.Cota);
                    }
                    _niveles[n.Nombre] = nivel;
                    if (tipoPlanta != null && !conVista.Contains(nivel.Id))
                        ViewPlan.Create(_doc, tipoPlanta.Id, nivel.Id);
                });
            }

            var ejes = U.De<Grid>(_doc).GroupBy(g => g.Name).ToDictionary(g => g.Key, g => g.First());
            foreach (var e in _m.Ejes)
            {
                Intentar("Eje " + e.Nombre, () =>
                {
                    var nombre = U.NombreSeguro(e.Nombre);
                    var linea = Line.CreateBound(U.P(e.Inicio), U.P(e.Fin));
                    if (ejes.TryGetValue(nombre, out var existente))
                    {
                        // Mismo eje de una importación anterior: se reutiliza; si está en otra posición se avisa
                        var c = existente.Curve;
                        var mismo = c is Line l && l.Direction.CrossProduct(linea.Direction).GetLength() < 1e-6
                                    && linea.Distance(new XYZ(c.GetEndPoint(0).X, c.GetEndPoint(0).Y, 0)) < U.M(0.01);
                        if (!mismo)
                            _inf.Advertencia($"Ya existe un eje '{nombre}' en otra posición; no se creó. Renómbralo o bórralo si sobra.");
                        return;
                    }
                    var g = Grid.Create(_doc, linea);
                    g.Name = nombre;
                    ejes[nombre] = g;
                    _inf.Contar("Ejes");
                });
            }
        }
    }
}

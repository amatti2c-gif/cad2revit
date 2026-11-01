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
                Etapa("Columnas", CrearColumnas);
                Etapa("Vigas", CrearVigas);
                Etapa("Fundaciones", CrearFundaciones);
                if (_m.Recintos.Count > 0) Etapa("Recintos", CrearRecintos);
                grupo.Assimilate();
            }
            ListarFamilias();
            return _inf;
        }

        private string _etapa = "";
        private string _ultimoFallo = "";

        /// <summary>Agrupa la creación de un tipo de elemento (para el informe).</summary>
        private void Etapa(string nombre, Action accion)
        {
            _etapa = nombre;
            try { accion(); }
            catch (Exception ex) { _inf.Error($"Etapa '{nombre}' abortada: {ex.Message}"); }
        }

        /// <summary>
        /// Crea UN elemento en su propia transacción. Si Revit lo rechaza al confirmar
        /// (p.ej. una familia que no cabe en el muro), se pierde solo ese elemento y se
        /// informa; antes, un rechazo deshacía la etapa completa sin aviso.
        /// </summary>
        private bool Intentar(string id, Action accion, bool registrarError = true)
        {
            var conteo = new Dictionary<string, int>(_inf.Creados);
            var fallas = new ManejadorFallas(_inf);
            using (var t = new Transaction(_doc, $"cad2revit: {_etapa} {id}"))
            {
                var op = t.GetFailureHandlingOptions();
                op.SetFailuresPreprocessor(fallas);
                op.SetClearAfterRollback(true);
                t.SetFailureHandlingOptions(op);
                t.Start();
                string motivo = null;
                try
                {
                    accion();
                    if (t.Commit() != TransactionStatus.Committed)
                        motivo = "Revit lo rechazó" + (fallas.Errores.Count > 0 ? ": " + string.Join(" | ", fallas.Errores) : "");
                }
                catch (Exception ex)
                {
                    if (t.HasStarted() && !t.HasEnded()) t.RollBack();
                    motivo = ex.Message;
                }
                if (motivo == null) return true;
                _inf.Creados.Clear();
                foreach (var kv in conteo) _inf.Creados[kv.Key] = kv.Value;
                _ultimoFallo = motivo;
                if (registrarError) _inf.Error($"{id}: {motivo}");
                return false;
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

        /// <summary>Ejecuta la creación de un elemento sin que un fallo detenga la etapa.</summary>
        private void Intentar(string id, Action accion)
        {
            try { accion(); }
            catch (Exception ex) { _inf.Error($"{id}: {ex.Message}"); }
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

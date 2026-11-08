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

        // ---------------------------------------------------------------- registro de ejes
        // Los ejes (Grid) no tienen parámetro "Comentarios", así que no se pueden marcar como el resto.
        // Se guarda la lista de los ejes creados en la información del proyecto (almacenamiento del add-in)
        // para poder borrarlos al reimportar.
        private static readonly Guid GuidEjes = new Guid("6f1c2a8e-3b7d-4c51-9a0e-2d4b8e7f1c39");

        private static Autodesk.Revit.DB.ExtensibleStorage.Schema EsquemaEjes()
        {
            var esquema = Autodesk.Revit.DB.ExtensibleStorage.Schema.Lookup(GuidEjes);
            if (esquema != null) return esquema;
            var sb = new Autodesk.Revit.DB.ExtensibleStorage.SchemaBuilder(GuidEjes);
            sb.SetSchemaName("cad2revit_ejes");
            sb.SetReadAccessLevel(Autodesk.Revit.DB.ExtensibleStorage.AccessLevel.Public);
            sb.SetWriteAccessLevel(Autodesk.Revit.DB.ExtensibleStorage.AccessLevel.Public);
            sb.AddSimpleField("ids", typeof(string));
            return sb.Finish();
        }

        // ---------------------------------------------------------------- origen del modelo
        // Los planos vienen en coordenadas de terreno (a kilómetros del origen). El modelo se traslada para que
        // la esquina del edificio quede cerca del origen de Revit. El desplazamiento se guarda en el proyecto
        // y se reutiliza al reimportar, para que todo siga calzando.
        private static readonly Guid GuidOrigen = new Guid("0b7e3f52-9c4d-4a61-8f2e-6d1a5c3b9e74");

        private static Autodesk.Revit.DB.ExtensibleStorage.Schema EsquemaOrigen()
        {
            var esquema = Autodesk.Revit.DB.ExtensibleStorage.Schema.Lookup(GuidOrigen);
            if (esquema != null) return esquema;
            var sb = new Autodesk.Revit.DB.ExtensibleStorage.SchemaBuilder(GuidOrigen);
            sb.SetSchemaName("cad2revit_origen");
            sb.SetReadAccessLevel(Autodesk.Revit.DB.ExtensibleStorage.AccessLevel.Public);
            sb.SetWriteAccessLevel(Autodesk.Revit.DB.ExtensibleStorage.AccessLevel.Public);
            sb.AddSimpleField("xy", typeof(string));
            return sb.Finish();
        }

        private void FijarOrigen()
        {
            U.OrigenX = U.OrigenY = 0;
            try
            {
                var ent = _doc.ProjectInformation.GetEntity(EsquemaOrigen());
                var guardado = ent != null && ent.IsValid() ? ent.Get<string>("xy") : null;
                if (!string.IsNullOrEmpty(guardado))
                {
                    var p = guardado.Split(';');
                    U.OrigenX = double.Parse(p[0], System.Globalization.CultureInfo.InvariantCulture);
                    U.OrigenY = double.Parse(p[1], System.Globalization.CultureInfo.InvariantCulture);
                    _inf.Advertencia($"Coordenadas: se usa el mismo origen de la importación anterior (punto {U.OrigenX:0.##}, {U.OrigenY:0.##} del plano en 0, 0)");
                    return;
                }
            }
            catch { /* sin origen guardado */ }
            var xs = _m.Muros.SelectMany(w => new[] { w.Inicio[0], w.Fin[0] }).Concat(_m.Ejes.SelectMany(e => new[] { e.Inicio[0], e.Fin[0] })).ToList();
            var ys = _m.Muros.SelectMany(w => new[] { w.Inicio[1], w.Fin[1] }).Concat(_m.Ejes.SelectMany(e => new[] { e.Inicio[1], e.Fin[1] })).ToList();
            if (xs.Count == 0) return;
            double x0 = xs.Min(), y0 = ys.Min();
            if (Math.Abs(x0) < 200 && Math.Abs(y0) < 200) return;        // ya está cerca del origen
            U.OrigenX = Math.Floor(x0 / 10) * 10;                         // múltiplos de 10 m: estable si el plano cambia un poco
            U.OrigenY = Math.Floor(y0 / 10) * 10;
            _inf.Advertencia($"Coordenadas: el plano está lejos del origen; el punto ({U.OrigenX:0}, {U.OrigenY:0}) del plano " +
                             "quedó en el origen de Revit (0, 0). Se reutiliza en las próximas importaciones.");
            using (var t = new Transaction(_doc, "cad2revit: origen"))
            {
                t.Start();
                var ent = new Autodesk.Revit.DB.ExtensibleStorage.Entity(EsquemaOrigen());
                ent.Set("xy", string.Format(System.Globalization.CultureInfo.InvariantCulture, "{0};{1}", U.OrigenX, U.OrigenY));
                _doc.ProjectInformation.SetEntity(ent);
                t.Commit();
            }
        }

        public static List<ElementId> EjesPrevios(Document doc)
        {
            var ent = doc.ProjectInformation.GetEntity(EsquemaEjes());
            if (ent == null || !ent.IsValid()) return new List<ElementId>();
            var texto = ent.Get<string>("ids") ?? "";
            return texto.Split(';', StringSplitOptions.RemoveEmptyEntries)
                        .Select(u => doc.GetElement(u)).Where(e => e is Grid).Select(e => e.Id).ToList();
        }

        private static void RegistrarEjes(Document doc, List<Grid> ejes)
        {
            var esquema = EsquemaEjes();
            var ent = new Autodesk.Revit.DB.ExtensibleStorage.Entity(esquema);
            ent.Set("ids", string.Join(";", ejes.Select(g => g.UniqueId)));
            doc.ProjectInformation.SetEntity(ent);
        }

        private string NombreLibre(string nombre)
        {
            var usados = new HashSet<string>(U.De<Grid>(_doc).Select(g => g.Name));
            var n = nombre;
            for (int i = 2; usados.Contains(n); i++) n = $"{nombre} {i}";
            return n;
        }

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
            FijarOrigen();
            if (Actualizar) LeerSincronizacion();

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
                _actual = null;
                Etapa("Sincronización", CerrarSincronizacion);
                grupo.Assimilate();
            }
            ListarFamilias();
            return _inf;
        }

        private string _etapa = "";
        private string _ultimoFallo = "";

        /// <summary>Mensaje de una excepción con lo necesario para diagnosticarla desde el informe:
        /// tipo, parámetro (si es de argumentos) y la línea del add-in donde ocurrió.</summary>
        private static string Detalle(Exception ex)
        {
            var e = ex is System.Reflection.TargetInvocationException tie && tie.InnerException != null ? tie.InnerException : ex;
            var txt = e.Message;
            if (e is ArgumentException ae && !string.IsNullOrEmpty(ae.ParamName) && !txt.Contains(ae.ParamName))
                txt += $" (parámetro '{ae.ParamName}')";
            var donde = (e.StackTrace ?? "").Split('\n').Select(l => l.Trim())
                        .FirstOrDefault(l => l.Contains("Cad2Revit."));
            if (!string.IsNullOrEmpty(donde))
                txt += $" [{e.GetType().Name} en {donde.Replace("at ", "").Split(" in ")[0]}]";
            return txt;
        }

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
            if (_actual is { } u) _registro.Add((e.Id, u.tipo, u.nivel, u.pts, u.def));
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
                    _ultimoFallo = Detalle(ex);
                    if (registrarError) _inf.Error($"{id}: {ex.Message}");
                    return false;
                }
            }
        }

        private void EliminarAnterior() => Intentar("eliminar importación anterior", () =>
        {
            var ids = ElementosPrevios(_doc);
            ids.AddRange(EjesPrevios(_doc));          // los ejes no tienen "Comentarios": se guardan aparte
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
            var creados = new List<Grid>();
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
                        if (mismo) { creados.Add(existente); return; }
                        // eje viejo con el mismo nombre (de una importación anterior, sin registro): se renombra
                        existente.Name = NombreLibre(nombre + " (anterior)");
                        _inf.Advertencia($"El eje '{nombre}' existente estaba en otra posición; se renombró a " +
                                         $"'{existente.Name}' y se creó el nuevo. Bórralo si sobra.");
                        ejes.Remove(nombre);
                    }
                    var g = Grid.Create(_doc, linea);
                    g.Name = nombre;
                    ejes[nombre] = g;
                    creados.Add(g);
                    _inf.Contar("Ejes");
                });
            }
            Intentar("registro de ejes", () => RegistrarEjes(_doc, creados), false);
        }
    }
}

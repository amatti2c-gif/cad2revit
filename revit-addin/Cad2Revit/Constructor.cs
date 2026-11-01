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
            return _inf;
        }

        private void Etapa(string nombre, Action accion)
        {
            using (var t = new Transaction(_doc, "cad2revit: " + nombre))
            {
                var op = t.GetFailureHandlingOptions();
                op.SetFailuresPreprocessor(new SilenciarAdvertencias(_inf));
                t.SetFailureHandlingOptions(op);
                t.Start();
                try
                {
                    accion();
                    t.Commit();
                }
                catch (Exception ex)
                {
                    t.RollBack();
                    _inf.Error($"Etapa '{nombre}' abortada: {ex.Message}");
                }
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

        private ElementId Material(string nombre) =>
            nombre != null && _materiales.TryGetValue(nombre, out var id) ? id : ElementId.InvalidElementId;

        // ------------------------------------------------------------ niveles
        private void CrearNivelesYEjes()
        {
            var existentes = U.De<Level>(_doc).ToDictionary(l => l.Name, l => l);
            var tipoPlanta = U.De<ViewFamilyType>(_doc).FirstOrDefault(v => v.ViewFamily == ViewFamily.FloorPlan);
            var conVista = new HashSet<ElementId>(U.De<ViewPlan>(_doc).Where(v => !v.IsTemplate && v.GenLevel != null)
                                                                       .Select(v => v.GenLevel.Id));
            foreach (var n in _m.Niveles)
            {
                Intentar("Nivel " + n.Nombre, () =>
                {
                    if (!existentes.TryGetValue(n.Nombre, out var nivel))
                    {
                        nivel = Level.Create(_doc, U.M(n.Cota));
                        nivel.Name = n.Nombre;
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

            var nombresEje = new HashSet<string>(U.De<Grid>(_doc).Select(g => g.Name));
            foreach (var e in _m.Ejes)
            {
                Intentar("Eje " + e.Nombre, () =>
                {
                    var g = Grid.Create(_doc, Line.CreateBound(U.P(e.Inicio), U.P(e.Fin)));
                    if (!nombresEje.Contains(e.Nombre)) { g.Name = e.Nombre; nombresEje.Add(e.Nombre); }
                    else _inf.Advertencia($"Ya existe un eje '{e.Nombre}'; se deja el nombre automático {g.Name}");
                    _inf.Contar("Ejes");
                });
            }
        }
    }
}

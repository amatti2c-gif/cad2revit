using System.Collections.Generic;
using System.IO;
using System.Text.Json;

namespace Cad2Revit
{
    // Espejo en C# de schema/modelo.schema.json. Las propiedades PascalCase se
    // mapean a snake_case del JSON (NivelSuperior <-> nivel_superior).

    public class Modelo
    {
        public string Version { get; set; }
        public string Unidades { get; set; }
        public string Proyecto { get; set; }
        public string SistemaEstructural { get; set; }
        public string MaterialEstructural { get; set; }
        public Lod Lod { get; set; }
        public List<MaterialDef> Materiales { get; set; } = new();
        public List<NivelDef> Niveles { get; set; } = new();
        public List<EjeDef> Ejes { get; set; } = new();
        public List<TipoDef> TiposMuro { get; set; } = new();
        public List<TipoDef> TiposLosa { get; set; } = new();
        public List<MuroDef> Muros { get; set; } = new();
        public List<AberturaDef> Aberturas { get; set; } = new();
        public List<LosaDef> Losas { get; set; } = new();
        public List<CieloDef> Cielos { get; set; } = new();
        public List<ColumnaDef> Columnas { get; set; } = new();
        public List<VigaDef> Vigas { get; set; } = new();
        public FundacionesDef Fundaciones { get; set; } = new();
        public List<RecintoDef> Recintos { get; set; } = new();
        public List<EntramadoDef> Entramados { get; set; } = new();
        public List<MarcoDef> Marcos { get; set; } = new();
        public List<string> Advertencias { get; set; } = new();

        public static Modelo Leer(string ruta)
        {
            var opciones = new JsonSerializerOptions
            {
                PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
                PropertyNameCaseInsensitive = true,
            };
            var m = JsonSerializer.Deserialize<Modelo>(File.ReadAllText(ruta), opciones);
            if (m == null || m.Unidades != "m")
                throw new InvalidDataException("El archivo no es un modelo cad2revit válido (unidades debe ser 'm').");
            return m;
        }
    }

    public class Lod
    {
        public int Preset { get; set; }
        public string Muros { get; set; }
        public string Aberturas { get; set; }
        public bool Columnas { get; set; }
        public bool Vigas { get; set; }
        public bool Fundaciones { get; set; }
        public bool Recintos { get; set; }
    }

    public class MaterialDef { public string Nombre { get; set; } public string Clase { get; set; } public int[] Color { get; set; } }
    public class NivelDef { public string Nombre { get; set; } public double Cota { get; set; } }
    public class EjeDef { public string Nombre { get; set; } public double[] Inicio { get; set; } public double[] Fin { get; set; } }

    public class CapaDef { public string Funcion { get; set; } public string Material { get; set; } public double Espesor { get; set; } }
    public class TipoDef { public string Nombre { get; set; } public bool Estructural { get; set; } public List<CapaDef> Capas { get; set; } = new(); }

    public class MuroDef
    {
        public string Id { get; set; }
        public string Nivel { get; set; }
        public string NivelSuperior { get; set; }
        public double? Altura { get; set; }
        /// <summary>Muro de buhardilla: su parte superior se une al techo (frontón).</summary>
        public bool UnirTecho { get; set; }
        public double[] Inicio { get; set; }
        public double[] Fin { get; set; }
        public double Espesor { get; set; }
        public string Tipo { get; set; }
        public bool Estructural { get; set; }
    }

    public class AberturaDef
    {
        public string Id { get; set; }
        public string Muro { get; set; }
        public string Categoria { get; set; }
        public string Modo { get; set; }
        public string Familia { get; set; }
        public string Tipo { get; set; }
        public double[] Centro { get; set; }
        public double Ancho { get; set; }
        public double Alto { get; set; }
        public double Antepecho { get; set; }
    }

    public class LosaDef
    {
        public string Id { get; set; }
        public string Nivel { get; set; }
        public string Tipo { get; set; }
        public bool Cubierta { get; set; }
        public bool Terminacion { get; set; }
        /// <summary>Cubierta inclinada: grados de pendiente y bordes del contorno que la llevan (0 = plana).</summary>
        public double Pendiente { get; set; }
        public List<int> BordesPendiente { get; set; } = new();
        public string Forma { get; set; }
        public double Desfase { get; set; }
        public List<double[]> Contorno { get; set; } = new();
    }

    public class CieloDef { public string Nivel { get; set; } public double Altura { get; set; } public List<double[]> Contorno { get; set; } = new(); }

    public class ColumnaDef
    {
        public string Material { get; set; }
        public string Forma { get; set; } = "rectangular";
        public string Id { get; set; }
        public string Nivel { get; set; }
        public string NivelSuperior { get; set; }
        public double? Altura { get; set; }
        public double[] Centro { get; set; }
        public double B { get; set; }
        public double H { get; set; }
        public double Rotacion { get; set; }
        public string Familia { get; set; }
        public string ParametroB { get; set; }
        public string ParametroH { get; set; }
    }

    public class VigaDef
    {
        public string Material { get; set; }
        public string Forma { get; set; } = "rectangular";
        public string Id { get; set; }
        public string Nivel { get; set; }
        public double[] Inicio { get; set; }
        public double[] Fin { get; set; }
        public double B { get; set; }
        public double H { get; set; }
        public string Familia { get; set; }
        public string ParametroB { get; set; }
        public string ParametroH { get; set; }
    }

    public class FundacionesDef
    {
        public List<CorridaDef> Corridas { get; set; } = new();
        public List<AisladaDef> Aisladas { get; set; } = new();
    }

    public class CorridaDef { public string Muro { get; set; } public double Ancho { get; set; } public double Alto { get; set; } }

    public class AisladaDef
    {
        public string Columna { get; set; }
        public double[] Centro { get; set; }
        public double B { get; set; }
        public double L { get; set; }
        public double H { get; set; }
        public string Familia { get; set; }
        public double CotaSello { get; set; }
    }

    public class EntramadoDef
    {
        public string Forma { get; set; } = "rectangular";
        public string Muro { get; set; }
        public string Nivel { get; set; }
        public string Material { get; set; }
        public double[] Direccion { get; set; }
        public double MetrosLineales { get; set; }
        public Dictionary<string, int> Resumen { get; set; } = new();
        public List<PiezaDef> Piezas { get; set; } = new();
        public RevitEntramadoDef Revit { get; set; } = new();
    }

    public class RevitEntramadoDef
    {
        public string Modo { get; set; } = "solido";
        public FamiliaTipoDef FamiliaVertical { get; set; }
        public FamiliaTipoDef FamiliaHorizontal { get; set; }
    }

    public class FamiliaTipoDef { public string Familia { get; set; } public string Tipo { get; set; } }

    public class PiezaDef
    {
        public string Rol { get; set; }
        public double[] Inicio { get; set; }
        public double[] Fin { get; set; }
        public double Ancho { get; set; }
        public double Profundidad { get; set; }
    }

    public class MarcoDef
    {
        public string Id { get; set; }
        public string Nivel { get; set; }
        public string NivelSuperior { get; set; }
        public string Tipo { get; set; }
        public string Sistema { get; set; }            // momento | arriostrado_x
        public double[] Inicio { get; set; }
        public double[] Fin { get; set; }
        public string Material { get; set; }
        public double[] Direccion { get; set; }
        public Dictionary<string, PerfilDef> Perfiles { get; set; } = new();
        public List<ColumnaMarcoDef> Columnas { get; set; } = new();
        public List<BarraDef> Vigas { get; set; } = new();
        public List<BarraDef> Diagonales { get; set; } = new();
    }

    public class PerfilDef
    {
        public string Perfil { get; set; }
        public string Forma { get; set; }
        public string Familia { get; set; }
        public string Material { get; set; }
        public double B { get; set; }
        public double H { get; set; }
    }

    public class ColumnaMarcoDef { public double[] Punto { get; set; } public double Z0 { get; set; } public double Z1 { get; set; } }
    public class BarraDef { public double[] Inicio { get; set; } public double[] Fin { get; set; } }

    public class RecintoDef { public string Nivel { get; set; } public string Nombre { get; set; } public double[] Punto { get; set; } }
}

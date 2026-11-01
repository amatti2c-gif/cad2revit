using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;

namespace Cad2Revit
{
    public partial class Constructor
    {
        /// <summary>
        /// Entramado de madera: cada muro genera UN elemento (DirectShape) que contiene todas sus
        /// piezas como sólidos con el material de la madera. Así el modelo no se llena de cientos
        /// de elementos y se puede ocultar/aislar el entramado de un muro con un clic.
        /// </summary>
        private void CrearEntramados()
        {
            if (_m.Entramados == null || _m.Entramados.Count == 0) return;
            var cat = new ElementId(BuiltInCategory.OST_StructuralFraming);
            if (!DirectShape.IsValidCategoryId(cat, _doc)) cat = new ElementId(BuiltInCategory.OST_GenericModel);

            foreach (var e in _m.Entramados)
            {
                Intentar("Entramado " + e.Muro, () =>
                {
                    var opciones = new SolidOptions(IdMaterial(e.Material), ElementId.InvalidElementId);
                    var normal = new XYZ(-e.Direccion[1], e.Direccion[0], 0);
                    var solidos = new List<GeometryObject>();
                    foreach (var p in e.Piezas)
                    {
                        var s = Pieza(p, normal, opciones);
                        if (s != null) solidos.Add(s);
                    }
                    if (solidos.Count == 0) throw new Exception("ninguna pieza válida");
                    var ds = DirectShape.CreateElement(_doc, cat);
                    ds.ApplicationId = "cad2revit";
                    ds.ApplicationDataId = "entramado " + e.Muro;
                    ds.SetShape(solidos);
                    try { ds.Name = U.NombreSeguro($"Entramado {e.Muro} ({e.Material})"); }
                    catch { /* algunas versiones no permiten nombrar DirectShapes */ }
                    Marcar(ds, $"entramado {e.Muro}: " +
                               string.Join(", ", e.Resumen.Select(kv => $"{kv.Value} {kv.Key}")) +
                               $"; {e.MetrosLineales:0.0} m lineales");
                    _inf.Contar("Piezas de entramado (madera)", solidos.Count);
                });
            }
        }

        /// <summary>Prisma de sección ancho x profundidad a lo largo del eje inicio→fin.</summary>
        private static Solid Pieza(PiezaDef p, XYZ normalMuro, SolidOptions opciones)
        {
            var a = new XYZ(U.M(p.Inicio[0]), U.M(p.Inicio[1]), U.M(p.Inicio[2]));
            var b = new XYZ(U.M(p.Fin[0]), U.M(p.Fin[1]), U.M(p.Fin[2]));
            var eje = b - a;
            var largo = eje.GetLength();
            if (largo < U.M(0.005)) return null;
            eje = eje.Normalize();
            var n = normalMuro.Normalize();
            var w = eje.CrossProduct(n).Normalize();      // lado "ancho", en el plano del muro
            double hw = U.M(p.Ancho) / 2, hn = U.M(p.Profundidad) / 2;
            var c = new[] { a - w * hw - n * hn, a + w * hw - n * hn, a + w * hw + n * hn, a - w * hw + n * hn };
            var perfil = new CurveLoop();
            for (int i = 0; i < 4; i++) perfil.Append(Line.CreateBound(c[i], c[(i + 1) % 4]));
            return GeometryCreationUtilities.CreateExtrusionGeometry(new List<CurveLoop> { perfil }, eje, largo, opciones);
        }
    }
}

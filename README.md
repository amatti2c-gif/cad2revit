# cad2revit

Convierte planos 2D de **AutoCAD** (plantas, elevaciones) en un **modelo 3D de Revit** con
muros, aberturas, losas, estructura y recintos. Qué se modela lo deciden tus propias
indicaciones sobre **materialidades**, **estructuración** y **nivel de detalle**.

```
 DWG ──(ODA File Converter)──► DXF ──► [parser Python] ──► modelo.json ──► [add-in Revit] ──► .rvt
                                          ▲
              entradas/*.yaml  ───────────┘   (tus indicaciones)

 ArchiCAD ──► IFC ──► Revit (Abrir IFC / Vincular IFC)   ← ver "ArchiCAD" más abajo
```

## 1. Tus indicaciones: carpeta `entradas/`

| Archivo | Qué defines |
|---|---|
| [`materialidades.yaml`](entradas/materialidades.yaml) | Catálogo de materiales (nombre, clase, color). Tipos de **muro, losa y cubierta** con sus capas (función, material, espesor). **Reglas** para asignar un tipo a cada muro según la capa del CAD y el espesor medido. |
| [`estructuracion.yaml`](entradas/estructuracion.yaml) | Sistema estructural. Criterios de **muro estructural** (espesor mínimo, capas, perímetro). **Columnas** (sección y familia), **vigas** (entre columnas alineadas o sobre muros), **losas** (cómo se obtiene el contorno), **fundaciones** (corridas o aisladas, sello) y **ejes**. |
| [`nivel_detalle.yaml`](entradas/nivel_detalle.yaml) | **Hasta dónde llega el modelo**: un preset LOD 100/200/300/350 que puedes ajustar elemento por elemento (muros genéricos/por tipo/compuestos, aberturas ninguna/hueco/familia, columnas, vigas, fundaciones, recintos, terminaciones, cielos…). |
| [`capas_cad.yaml`](entradas/capas_cad.yaml) | Correspondencia **capa CAD → categoría** y **bloque CAD → familia de Revit**, más parámetros de detección de muros. |
| [`proyecto.yaml`](entradas/proyecto.yaml) | Niveles, qué planta corresponde a cada nivel y qué elevaciones leer. |

Los archivos traen comentarios y valores de ejemplo. El parser revisa que sean coherentes
(materiales inexistentes, tipos mal referenciados, claves de LOD inválidas) y avisa en español
antes de procesar.

### Qué hace cada nivel de detalle

| Elemento | 100 | 200 | 300 | 350 |
|---|---|---|---|---|
| Niveles, ejes | ✔ | ✔ | ✔ | ✔ |
| Muros | genérico por espesor | tipo + material de núcleo | compuesto por capas | compuesto por capas |
| Puertas / ventanas | — | huecos | familias | familias |
| Losas / cubierta | ✔ | ✔ | ✔ (compuestas) | estructura + terminación separadas |
| Columnas | — | ✔ | ✔ | ✔ |
| Vigas | — | — | ✔ | ✔ |
| Fundaciones | — | — | ✔ | ✔ |
| Recintos | — | — | ✔ | ✔ |
| Cielos | — | — | — | ✔ |

**Nunca se modela automáticamente:** armaduras, uniones metálicas, instalaciones (MEP),
mobiliario ni detalles constructivos 2D. Los detalles sirven de referencia para escribir las
capas en `materialidades.yaml`.

## 2. Parser (Python)

```bash
cd parser
pip install -e ".[dev]"
```

```bash
python ../ejemplos/generar_ejemplo.py          # planos DXF de prueba (casa de 2 pisos)
```

```bash
python -m cad2revit ../entradas/proyecto.yaml -o modelo.json --validar
```

Imprime un resumen (muros, aberturas, losas, etc.) y una lista de **advertencias** sobre lo que
no se pudo interpretar o lo que conviene revisar.

**DWG → DXF:** el parser lee DXF. Para convertir DWG en lote usa
[ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter) (gratuito),
o en AutoCAD: `GUARDARCOMO → DXF`.

### Cómo interpreta los planos
1. **Muros:** busca pares de líneas paralelas en capas de muro, separadas entre `espesor_min` y
   `espesor_max`. El eje del muro es la línea media. Une tramos cortados por uniones en T o por
   vanos de puertas y ventanas, y extiende los extremos para resolver esquinas.
2. **Aberturas:** toma los bloques de puerta y ventana (por nombre o por capa) y los aloja en el
   muro más cercano. El ancho se lee de `capas_cad.yaml` o se mide del bloque.
3. **Columnas:** bloques o polilíneas cerradas en la capa de columnas. Mide la sección y la
   rotación.
4. **Niveles:** lee textos tipo `NPT +2.70` de las elevaciones. Si su cantidad coincide con los
   niveles del proyecto, reemplaza las cotas.
5. **Tipos, estructura y LOD:** aplica tus reglas y decide qué se exporta. El JSON resultante
   ([esquema](schema/modelo.schema.json)) ya viene "resuelto": el add-in no toma decisiones.

### Requisitos para buenos resultados
- Capas consistentes: muros, tabiques, puertas, ventanas y columnas en capas distintas.
- Puertas y ventanas como **bloques** (no descompuestos).
- Dibujo a escala real 1:1 con unidades declaradas (`$INSUNITS`) o `unidades:` en `capas_cad.yaml`.
- Todas las plantas con el mismo origen de coordenadas.

## 3. Add-in de Revit (C#)

Requiere Visual Studio 2022 o el .NET SDK 8, y Revit instalado.

```bash
cd revit-addin/Cad2Revit
dotnet build -c Release                         # Revit 2025 (por defecto)
```

```bash
dotnet build -c Release -p:RevitVersion=2024    # Revit 2024 (.NET Framework 4.8)
```

Al compilar se copia el add-in a `%AppData%\Autodesk\Revit\Addins\<versión>\`. En Revit aparece
la pestaña **cad2revit → Construir modelo**: eliges `modelo.json` y se crean, en este orden,
materiales y tipos compuestos, niveles (con sus vistas de planta), ejes, muros, puertas y
ventanas (o huecos), losas y cubierta, cielos, columnas, vigas, fundaciones y recintos.

- Todo queda en **un solo paso de deshacer** (TransactionGroup).
- Cada elemento lleva en *Comentarios* su id de origen (`cad2revit N1-M003`) para rastrearlo
  hasta el CAD.
- Al terminar se muestra un informe, que también se guarda como `modelo.informe.txt`.
- **Familias:** las de puertas, ventanas, columnas, vigas y zapatas indicadas en los YAML deben
  estar **cargadas en la plantilla** del proyecto. Si una falta, se usa la primera disponible de
  esa categoría y se avisa. Los tipos con medidas nuevas se duplican automáticamente
  (por ejemplo `0915 x 2134mm [90x210]`).

## 4. ArchiCAD
ArchiCAD ya trabaja en 3D, así que **no hay que reconstruir** el modelo: exporta a **IFC 4**
(traductor "Revit" de ArchiCAD) y en Revit usa *Abrir → IFC*. Para mapear tipos de ArchiCAD a
los de Revit, usa la tabla de mapeo IFC de Revit. Si solo tienes DWG exportados desde ArchiCAD,
usa el flujo de este repositorio como con cualquier DWG.

## 5. Estructura del repositorio

```
entradas/          tus indicaciones (YAML comentados)
parser/cad2revit/  lectura DXF, detección de muros/aberturas/columnas, reglas, LOD, JSON
parser/tests/      pruebas de punta a punta con planos generados
schema/            contrato JSON entre parser y add-in
revit-addin/       add-in C# (Revit API)
ejemplos/          generador de planos de prueba
```

## 6. Hoja de ruta
- [ ] Muros curvos (arcos) y muros con altura variable desde elevaciones.
- [ ] Escaleras y rampas desde la planta.
- [ ] Cubiertas inclinadas a partir de cortes y elevaciones.
- [ ] Lectura directa de DWG (ODA SDK) sin pasar por DXF.
- [ ] Interfaz de revisión: mostrar sobre el CAD lo que se detectó antes de construir.
- [ ] Planos sin estándar o escaneados (visión por computador o machine learning).

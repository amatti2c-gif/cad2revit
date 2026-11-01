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

## 4. Aplicación web (trabajar e iterar desde el navegador)

La app permite hacer todo el ciclo sin usar la terminal:

1. **Planos:** subes los DXF arrastrándolos. Ves sus capas y bloques (útil para escribir
   `capas_cad.yaml`) y una vista previa.
2. **Lineamientos:** editas los 5 YAML en el navegador. Al guardar se validan y los errores
   aparecen en español.
3. **Ejecutar:** procesa los planos y registra la corrida junto con una nota de qué cambiaste.
4. **Resultado:** conteos con la diferencia respecto de la corrida anterior, advertencias,
   **planta 2D** (lo detectado sobre el CAD original, con el id, tipo y espesor de cada elemento
   al pasar el cursor) y **modelo 3D** navegable. Desde aquí descargas `modelo.json` para Revit.
5. **Historial:** todas las corridas con sus notas. Puedes **comparar** dos corridas (qué
   lineamientos cambiaron y cómo cambiaron los conteos) y **restaurar** los lineamientos de una
   corrida anterior.

### Ejecutar localmente

```bash
pip install -e parser -r app/requirements.txt
```

```bash
uvicorn app.servidor:app --reload
```

Luego abre http://localhost:8000. Los proyectos se guardan en `./datos/` (configurable con
`CAD2REVIT_DATOS`).

### Publicarla en internet (Railway)

Usa el mismo esquema que `monitoreo-financiero`: un Dockerfile, un `railway.toml` y un volumen
persistente.

1. En Railway: **New Project → Deploy from GitHub repo → `amatti2c-gif/cad2revit`**.
2. En **Variables**, define `APP_USERNAME` y `APP_PASSWORD`. Son obligatorias: si faltan, la app
   responde "Acceso bloqueado" en vez de quedar abierta.
3. Revisa que exista el **volumen** montado en `/datos`. Lo crea `railway.toml`; si no aparece,
   agrégalo con clic derecho en el servicio → *Attach volume* → `/datos`. Ahí se guardan
   proyectos, planos e historial.
4. En **Settings → Networking**, usa **Generate Domain** para obtener un link
   `*.up.railway.app`, o **Custom Domain** para usar un subdominio propio
   (por ejemplo `cad2revit.tudominio.cl`) agregando el registro CNAME que indica Railway.

Cada push a `main` se publica automáticamente. Para usarla en local no hacen falta las
variables: sin ellas, la app queda abierta.

> El paso final a Revit sigue siendo local, porque Revit solo corre en Windows: descargas
> `modelo.json` y usas el add-in. Para generar el `.rvt` en la nube habría que integrar
> Autodesk Platform Services (Design Automation for Revit), que requiere cuenta y créditos de
> Autodesk.

## 5. ArchiCAD
ArchiCAD ya trabaja en 3D, así que **no hay que reconstruir** el modelo: exporta a **IFC 4**
(traductor "Revit" de ArchiCAD) y en Revit usa *Abrir → IFC*. Para mapear tipos de ArchiCAD a
los de Revit, usa la tabla de mapeo IFC de Revit. Si solo tienes DWG exportados desde ArchiCAD,
usa el flujo de este repositorio como con cualquier DWG.

## 6. Estructura del repositorio

```
entradas/          tus indicaciones (YAML comentados)
parser/cad2revit/  lectura DXF, detección de muros/aberturas/columnas, reglas, LOD, JSON
parser/tests/      pruebas de punta a punta con planos generados
schema/            contrato JSON entre parser y add-in
revit-addin/       add-in C# (Revit API)
app/               aplicación web (FastAPI + interfaz HTML/JS, visor 2D/3D)
ejemplos/          generador de planos de prueba
```

## 7. Hoja de ruta
- [ ] Muros curvos (arcos) y muros con altura variable desde elevaciones.
- [ ] Escaleras y rampas desde la planta.
- [ ] Cubiertas inclinadas a partir de cortes y elevaciones.
- [ ] Lectura directa de DWG (ODA SDK) sin pasar por DXF.
- [x] Interfaz de revisión: mostrar sobre el CAD lo que se detectó antes de construir.
- [ ] Generar el .rvt en la nube (Autodesk Platform Services, Design Automation).
- [ ] Formularios guiados para los lineamientos (además del editor YAML).
- [ ] Planos sin estándar o escaneados (visión por computador o machine learning).

# MTGO Replay Logs

Genera, para cada partida de Magic Online, un log **acción por acción** con el estado de la mesa
después de cada acción: permanentes (con contadores y atacantes), cementerios, exilio, vidas,
cartas en mano/biblioteca, cartas conocidas en mano y la pila.

## Uso

Doble clic en `generar_logs.bat`, o desde una terminal en esta carpeta:

```
py -m mtgo_replay                 # las 10 últimas partidas
py -m mtgo_replay --last 25       # las 25 últimas
py -m mtgo_replay --match 78a8    # solo el match cuyo id empieza por 78a8
py -m mtgo_replay --archive-only  # solo guarda las "fotos" de mtgo.log (ver abajo)
```

Solo necesita Python 3.10+ (sin dependencias). Las carpetas de MTGO se detectan solas.

## Modo automático

```
py -m mtgo_replay --watch         # o doble clic en vigilar.bat (con consola)
```

`vigilar_mtgo.pyw` hace lo mismo sin ventana (ideal para arrancar con Windows). Cada 30 s:

* guarda las "fotos" de `mtgo.log` antes de que MTGO las borre,
* busca matches nuevos o modificados (también en carpetas nuevas que cree MTGO al actualizarse),
* cuando un match termina ("wins the match", o 10 min sin cambios si alguien se desconecta),
  genera sus logs en `output/`.

La primera vez marca todo el historial como ya visto; solo procesa lo que se juegue después.
Solo puede haber una copia en marcha. Registro de actividad: `data/watch.log`.
Opciones: `--interval 30` (segundos entre comprobaciones), `--idle 10` (minutos).

## Salida

`output/<fecha>_vs_<rival>/gameN.txt` y `gameN.json` (un archivo por partida del BO3).

* `.txt`: para leer. Cada acción del log seguida del estado resultante.
* `.json`: los mismos datos estructurados (pensado para un futuro frontend).

Leyenda del `.txt`:

| Marca        | Significado |
|--------------|-------------|
| `(inferred)` | No aparece en el log, pero se deduce de las reglas (p. ej. Fatal Push resolvió → criatura destruida). |
| `⚠`          | Estimación o suposición (daño de combate, qué criatura se sacrificó, correcciones tardías). |
| `≈`          | Valor aproximado (vida tras combate o tierras de choque; biblioteca rival `~`). |
| `[exact]`    | Estado copiado de la "foto" exacta que guarda MTGO (ver abajo). |

## De dónde salen los datos

1. **`Match_GameLog_*.dat`** (siempre disponible): el registro de eventos de MTGO. No dice qué robas,
   cuándo resuelve un hechizo, cuándo muere una criatura, qué tierra buscó una fetch ni la vida.
   El programa lo reconstruye:
   * resolución de la pila deducida de lo que pasa después,
   * efectos de hechizos/habilidades según su texto oracle (destruir, exiliar, devolver, daño, wipes, edictos, búsquedas…),
   * daño de combate estimado con fuerza/resistencia y contadores,
   * MTGO da un id nuevo y creciente a cada carta cada vez que cambia de zona: cuando más tarde aparece
     una carta con un id "inesperado", se corrige el pasado (p. ej. qué tierra trajo una fetch).
2. **Base de cartas de MTGO** (`CardDataSource`, offline): nombres, tipos, F/R, lealtad y texto oracle
   con los mismos ids que usa el juego. Se cachea en `data/cards.json`.
3. **Tus mazos guardados** (`grouping *.xml`): se adivina qué mazo usaste en cada match.
4. **`mtgo.log`** (opcional, pero muy valioso): MTGO escribe ahí "fotos" exactas del estado
   (vidas, tu mano, todas las zonas) y tu lista exacta. **MTGO borra ese archivo cada vez que se abre**,
   así que el programa lo archiva en `data/clientlogs/` cada vez que se ejecuta. Las partidas con
   fotos archivadas salen mucho más precisas (vida y mano exactas).
   → Ejecuta el programa (o `--archive-only`) **antes de volver a abrir MTGO** para no perderlas.

## Precisión (medida contra las fotos exactas de MTGO, 5 partidas)

Solo con el `.dat`: biblioteca 96 %, número de cartas en mano 88 %, zonas públicas idénticas en el 80 %
de los estados (0,5 cartas de diferencia de media). La vida es lo más difícil (tierras de choque
buscadas con fetch que nunca se nombran, momento exacto del daño de combate): exacta en ~34 %, por eso
se marca con `≈`. Con fotos de `mtgo.log` los valores pasan a ser exactos.

## Estructura del código

```
mtgo_replay/
  paths.py      localizar carpetas de MTGO
  gamelog.py    leer los .dat (binario) y separar las partidas del match
  events.py     convertir cada línea del log en un evento estructurado
  carddb.py     base de cartas offline de MTGO
  engine.py     motor de reconstrucción del estado
  clientlog.py  archivar/leer mtgo.log (fotos exactas y listas)
  snapshots.py  fusionar las fotos exactas con la reconstrucción
  decks.py      mazos guardados y adivinar el mazo usado
  render.py     generar .txt y .json
```

## Licencia

[MIT](LICENSE): cualquiera puede usar, copiar, modificar y distribuir este código libremente,
manteniendo el aviso de copyright.

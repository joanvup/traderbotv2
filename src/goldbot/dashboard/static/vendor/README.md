# Librería de gráficos vendorizada

`uPlot` 1.6.32 (MIT, © Leon Sorokin) copiada aquí una sola vez. El dashboard **no usa CDN**:
funciona sin internet. Licencia en `LICENSE-uPlot.txt`.

| Fichero | Bytes | SHA-256 (primeros 32) |
|---|---:|---|
| `uPlot.iife.min.js` | 51 081 | `19c8d4c6ad88929a79f4ae49d6f71615` |
| `uPlot.min.css` | 1 857 | `df630c6a8d6f8eeaff264b50f73ce5b1` |

Descarga (verificable):

```bash
curl -sfL -o uPlot.iife.min.js https://cdn.jsdelivr.net/npm/uplot@1.6.32/dist/uPlot.iife.min.js
curl -sfL -o uPlot.min.css     https://cdn.jsdelivr.net/npm/uplot@1.6.32/dist/uPlot.min.css
curl -sfL -o LICENSE-uPlot.txt https://cdn.jsdelivr.net/npm/uplot@1.6.32/LICENSE
```

Se elige uPlot sobre Chart.js por dos razones concretas: 51 KB en vez de ~200 KB y soporte
nativo de `null` en las series, que es como el panel de equity pinta los huecos de conexión
con MT5 sin interpolar (requisito de la especificación).

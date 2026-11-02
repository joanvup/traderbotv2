-- Fase 5: el panel de posiciones abiertas necesita precio actual y PnL en dinero.
--
-- `positions` guardaba la foto estructural (entrada, SL/TP, R, banderas de gestión) pero
-- no el flotante: el dashboard no puede llamar a MT5, así que sin estas dos columnas
-- tendría que reconstruir el PnL asumiendo el tamaño de contrato, algo prohibido por la
-- especificación. El bot las rellena cada ciclo con lo que reporta el broker.

ALTER TABLE positions ADD COLUMN current_price REAL;
ALTER TABLE positions ADD COLUMN pnl_money REAL;

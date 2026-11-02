-- Fase 4: la gestión debe sobrevivir a un reinicio del proceso.
--
-- `breakeven_done` y `partials_done` son el histórico de lo que ESTE bot ya hizo sobre
-- una posición. Sin ellos, tras un reinicio el TradeManager volvería a ver la posición
-- "sin gestionar" y repetiría un cierre parcial (o un movimiento de SL) que ya ocurrió,
-- cambiando el plan de salida de la operación en curso.

ALTER TABLE positions ADD COLUMN breakeven_done INTEGER NOT NULL DEFAULT 0;
ALTER TABLE positions ADD COLUMN partials_done INTEGER NOT NULL DEFAULT 0;

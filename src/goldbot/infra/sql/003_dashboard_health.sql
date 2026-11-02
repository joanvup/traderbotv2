-- Fase 5: el dashboard publica chips sobre el estado del enlace y del trading algorítmico.
--
-- `health_json` guarda la última BrokerHealth leída por el bot (terminal conectado,
-- algo trading, trade_allowed de la cuenta, latencia y error). El dashboard no llama a
-- MT5: es un proceso separado que solo lee SQLite, así que sin esta columna no podría
-- distinguir "algo trading apagado" de "el bot lleva 40 minutos sin hablar".

ALTER TABLE bot_state ADD COLUMN health_json TEXT;

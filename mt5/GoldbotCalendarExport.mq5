//+------------------------------------------------------------------+
//| GoldbotCalendarExport.mq5                                        |
//| Exporta el calendario economico nativo de MT5 a CSV para goldbot.|
//| El CSV se publica en <Terminal>\Common\Files\<subcarpeta>\...    |
//| Solo lectura del calendario: no envia ordenes ni toca la cuenta. |
//+------------------------------------------------------------------+
#property copyright "goldbot"
#property version   "1.10"
#property description "Exporta el calendario economico nativo de MT5 a CSV (Common\\Files) para goldbot."

input int    InpDaysBack     = 30;   // Dias hacia atras (CSV en vivo)
input int    InpDaysForward  = 14;   // Dias hacia adelante
input int    InpTimerSeconds = 60;   // Intervalo de exportacion (segundos)
input int    InpChunkDays    = 20;   // Dias por peticion al calendario
input int    InpMaxExportSeconds = 25; // Presupuesto de tiempo por exportacion
input bool   InpHistoryEnabled   = true;  // Segundo CSV con historico profundo
input int    InpHistoryDaysBack  = 730;   // Dias hacia atras del historico
input int    InpHistoryRefreshMinutes = 60; // Refresco del historico (minutos)
input string InpSubfolder    = "goldbot"; // Subcarpeta dentro de Common\Files
input string InpFileName     = "calendar_events.csv";
input string InpHistoryFileName = "calendar_history.csv";
input bool   InpVerbose      = true;

struct ExportStats
{
   int      chunks_ok;
   int      chunks_empty;
   int      chunks_failed;
   int      last_error;
   bool     truncated;
   datetime oldest_event;
   datetime newest_event;
};

datetime g_last_history_server = 0;

//+------------------------------------------------------------------+
//| Ruta del CSV dentro del sandbox comun.                           |
//+------------------------------------------------------------------+
string CsvPath(const string file_name)
{
   return InpSubfolder + "\\" + file_name;
}

//+------------------------------------------------------------------+
//| Nombre legible del impacto.                                      |
//+------------------------------------------------------------------+
string ImpactName(const ENUM_CALENDAR_EVENT_IMPACT impact)
{
   switch((int)impact)
   {
      case 0: return "none";      // CALENDAR_IMPORTANCE_NONE
      case 1: return "low";       // CALENDAR_IMPORTANCE_LOW
      case 2: return "moderate";  // CALENDAR_IMPORTANCE_MODERATE
      case 3: return "high";      // CALENDAR_IMPORTANCE_HIGH
   }
   return "unknown";
}

//+------------------------------------------------------------------+
//| Nombre legible del tipo de evento.                               |
//+------------------------------------------------------------------+
string EventTypeName(const ENUM_CALENDAR_EVENT_TYPE type)
{
   switch((int)type)
   {
      case 0: return "event";      // CALENDAR_TYPE_EVENT
      case 1: return "indicator";  // CALENDAR_TYPE_INDICATOR
      case 2: return "holiday";    // CALENDAR_TYPE_HOLIDAY
   }
   return "unknown";
}

//+------------------------------------------------------------------+
//| Elimina el delimitador y los saltos de linea de los textos.      |
//+------------------------------------------------------------------+
string Sanitize(string text)
{
   StringReplace(text, ";", ",");
   StringReplace(text, "\r", " ");
   StringReplace(text, "\n", " ");
   return text;
}

//+------------------------------------------------------------------+
//| Pide una franja al calendario. Las ventanas anchas dan           |
//| ERR_CALENDAR_TIMEOUT (5401), asi que se parten por la mitad.     |
//+------------------------------------------------------------------+
int FetchRange(MqlCalendarValue &out[], const datetime dfrom, const datetime dto,
               const int depth, ExportStats &stats)
{
   if(dto <= dfrom)
      return 0;

   MqlCalendarValue part[];
   ResetLastError();
   const int n = CalendarValueHistory(part, dfrom, dto, NULL, NULL);
   if(n >= 0)
   {
      const int base = ArraySize(out);
      ArrayResize(out, base + n);
      for(int i = 0; i < n; i++)
         out[base + i] = part[i];
      return n;
   }

   const int err = GetLastError();
   const long span = (long)dto - (long)dfrom;
   if(err == 5401 && depth > 0 && span > 4 * 86400)
   {
      const datetime mid = dfrom + (datetime)(span / 2);
      const int left  = FetchRange(out, dfrom, mid - 1, depth - 1, stats);
      if(left < 0)
         return left;
      const int right = FetchRange(out, mid, dto, depth - 1, stats);
      if(right < 0)
         return right;
      return left + right;
   }

   stats.last_error = err;
   return -err;
}

//+------------------------------------------------------------------+
//| Recorre la ventana de lo mas reciente a lo mas antiguo: si el    |
//| presupuesto de tiempo se agota, lo capturado sigue siendo la     |
//| franja que usa el filtro de noticias en vivo.                    |
//+------------------------------------------------------------------+
int CollectWindow(const datetime from, const datetime to,
                  MqlCalendarValue &out[], ExportStats &stats)
{
   const long chunk_s = (long)InpChunkDays * 86400;
   const ulong started = GetTickCount();

   stats.chunks_ok = 0;
   stats.chunks_empty = 0;
   stats.chunks_failed = 0;
   stats.last_error = 0;
   stats.truncated = false;
   stats.oldest_event = 0;
   stats.newest_event = 0;

   datetime cursor = to;
   while(cursor > from)
   {
      datetime slice_from = cursor - (datetime)chunk_s;
      if(slice_from < from)
         slice_from = from;

      const int added = FetchRange(out, slice_from, cursor, 3, stats);
      if(added < 0)
         stats.chunks_failed++;
      else
      {
         stats.chunks_ok++;
         if(added == 0)
            stats.chunks_empty++;
      }

      cursor = slice_from - 1;
      if(cursor <= from)
         break;
      if(((GetTickCount() - started) / 1000) >= (ulong)InpMaxExportSeconds)
      {
         stats.truncated = true;
         break;
      }
   }

   const int total = ArraySize(out);
   for(int i = 0; i < total; i++)
   {
      const datetime t = out[i].time;
      if(stats.oldest_event == 0 || t < stats.oldest_event)
         stats.oldest_event = t;
      if(stats.newest_event == 0 || t > stats.newest_event)
         stats.newest_event = t;
   }
   return total;
}

//+------------------------------------------------------------------+
//| Publica el CSV de forma atomica: escribe .tmp y lo renombra.     |
//| Si falla, el CSV anterior queda intacto (el bot lo vera viejo).  |
//+------------------------------------------------------------------+
bool WriteCsv(const string path, const MqlCalendarValue &values[],
              const datetime now_server, const datetime now_gmt, const ExportStats &stats)
{
   const string tmp = path + ".tmp";
   // FILE_ANSI + CP_UTF8 produce UTF-8 sin BOM (FILE_UTF8 no existe en este build).
   const int handle = FileOpen(tmp, FILE_WRITE | FILE_CSV | FILE_ANSI | FILE_COMMON, ';', CP_UTF8);
   if(handle == INVALID_HANDLE)
   {
      Print("goldbot: no se pudo abrir ", tmp, " (err=", GetLastError(), ")");
      return false;
   }

   FileWrite(handle,
             "schema_version", "value_id", "event_id",
             "event_time_server_epoch", "event_time_utc_epoch",
             "export_gmt_epoch", "export_server_epoch", "server_offset_s",
             "currency", "impact", "impact_name", "event_type", "event_type_name",
             "event_name", "event_code", "country_code",
             "actual_raw", "prev_raw", "forecast_raw",
             "digits", "unit", "period_server_epoch");

   const long offset = (long)now_server - (long)now_gmt;
   const int total = ArraySize(values);
   int written = 0;

   for(int i = 0; i < total; i++)
   {
      MqlCalendarEvent event;
      if(!CalendarEventById(values[i].event_id, event))
         continue;

      MqlCalendarCountry country;
      if(!CalendarCountryById(event.country_id, country))
         ZeroMemory(country);

      // Los importes se guardan escalados por 1e6; LONG_MIN significa "sin dato".
      const long event_server = (long)values[i].time;
      FileWrite(handle,
                1,
                (string)values[i].id,
                (string)values[i].event_id,
                (string)event_server,
                (string)(event_server - offset),
                (string)(long)now_gmt,
                (string)(long)now_server,
                (string)offset,
                Sanitize(country.currency),
                (string)(int)values[i].impact_type,
                ImpactName(values[i].impact_type),
                (string)(int)event.type,
                EventTypeName(event.type),
                Sanitize(event.name),
                Sanitize(event.event_code),
                Sanitize(country.code),
                (string)values[i].actual_value,
                (string)values[i].prev_value,
                (string)values[i].forecast_value,
                (string)event.digits,
                (string)(int)event.unit,
                (string)(long)values[i].period);
      written++;
   }

   FileClose(handle);

   if(!FileMove(tmp, FILE_COMMON, path, FILE_COMMON | FILE_REWRITE))
   {
      Print("goldbot: no se pudo publicar ", path, " (err=", GetLastError(), ")");
      return false;
   }

   if(InpVerbose)
   {
      string detalle = StringFormat("trozos ok=%d vacios=%d fallidos=%d",
                                    stats.chunks_ok, stats.chunks_empty, stats.chunks_failed);
      if(stats.last_error != 0)
         detalle += StringFormat(" err=%d", stats.last_error);
      if(stats.truncated)
         detalle += " (cortado por presupuesto)";
      Print("goldbot: ", path, " ", written, " eventos (",
            TimeToString(stats.oldest_event, TIME_DATE), " -> ",
            TimeToString(stats.newest_event, TIME_DATE), ") ", detalle,
            ", offset servidor ", offset, "s");
   }
   return true;
}

//+------------------------------------------------------------------+
//| Exporta una ventana concreta a un CSV.                           |
//+------------------------------------------------------------------+
void ExportWindow(const string file_name, const int days_back,
                  const datetime now_server, const datetime now_gmt)
{
   const datetime from = now_server - (datetime)((long)days_back * 86400);
   const datetime to   = now_server + (datetime)((long)InpDaysForward * 86400);

   MqlCalendarValue values[];
   ExportStats stats;
   const int total = CollectWindow(from, to, values, stats);
   if(total <= 0)
   {
      Print("goldbot: ", file_name, " omitido: 0 eventos (ok=", stats.chunks_ok,
            " vacios=", stats.chunks_empty, " fallidos=", stats.chunks_failed,
            " err=", stats.last_error, ")");
      return;
   }
   WriteCsv(CsvPath(file_name), values, now_server, now_gmt, stats);
}

void ExportLive()
{
   const datetime now_server = TimeTradeServer();
   const datetime now_gmt    = TimeGMT();
   if(now_server <= 0 || now_gmt <= 0)
   {
      Print("goldbot: sin hora de servidor/GMT; export omitido");
      return;
   }
   ExportWindow(InpFileName, InpDaysBack, now_server, now_gmt);
}

void ExportHistory()
{
   const datetime now_server = TimeTradeServer();
   const datetime now_gmt    = TimeGMT();
   if(now_server <= 0 || now_gmt <= 0)
      return;
   ExportWindow(InpHistoryFileName, InpHistoryDaysBack, now_server, now_gmt);
}

//+------------------------------------------------------------------+
//| Ciclo de vida                                                     |
//+------------------------------------------------------------------+
int OnInit()
{
   if(InpTimerSeconds < 5)
   {
      Print("goldbot: InpTimerSeconds debe ser >= 5");
      return INIT_PARAMETERS_INCORRECT;
   }
   if(InpDaysBack < 0 || InpDaysForward < 0 || InpHistoryDaysBack < 0)
   {
      Print("goldbot: los dias de la ventana no pueden ser negativos");
      return INIT_PARAMETERS_INCORRECT;
   }
   if(InpChunkDays < 1 || InpChunkDays > 365)
   {
      Print("goldbot: InpChunkDays debe estar entre 1 y 365");
      return INIT_PARAMETERS_INCORRECT;
   }
   if(InpMaxExportSeconds < 1)
   {
      Print("goldbot: InpMaxExportSeconds debe ser >= 1");
      return INIT_PARAMETERS_INCORRECT;
   }
   if(InpHistoryRefreshMinutes < 1)
   {
      Print("goldbot: InpHistoryRefreshMinutes debe ser >= 1");
      return INIT_PARAMETERS_INCORRECT;
   }

   EventSetTimer(InpTimerSeconds);
   ExportLive();
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   g_last_history_server = 0;
}

void OnTimer()
{
   ExportLive();
   if(!InpHistoryEnabled || InpHistoryDaysBack <= InpDaysBack)
      return;

   const datetime now_server = TimeTradeServer();
   if(g_last_history_server != 0 &&
      (now_server - g_last_history_server) < (datetime)(InpHistoryRefreshMinutes * 60))
      return;

   g_last_history_server = now_server;
   ExportHistory();
}
//+------------------------------------------------------------------+

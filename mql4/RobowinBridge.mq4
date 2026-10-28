//+------------------------------------------------------------------+
//| RobowinBridge.mq4 - ponte entre o MetaTrader 4 e o robowin (Python)|
//+------------------------------------------------------------------+
//
//  *** AVISO IMPORTANTE: ESTE ARQUIVO NAO FOI COMPILADO NEM TESTADO. ***
//  Quem o escreveu NAO tinha MetaEditor, MT4 nem Windows. O codigo foi
//  escrito de forma conservadora e revisado a mao, mas pode conter erros
//  de sintaxe ou de uso da biblioteca ZeroMQ. Antes de qualquer uso:
//    1. compile no MetaEditor (F7) e corrija os erros apontados;
//    2. teste SOMENTE em conta DEMO, com SomenteLeitura=true primeiro;
//    3. confira cada ponto marcado com "CONFERIR" abaixo;
//    4. rode o diagnostico do robowin (robowin diagnostico --plataforma mt4).
//  O lado Python (src/robowin/mt5/conector_mt4.py) e testado contra um
//  DUBLE que fala este mesmo protocolo (mql4/PROTOCOLO.md), o que NAO
//  prova que este EA funciona.
//
//  O que faz: abre um socket ZeroMQ REP em 127.0.0.1 e atende comandos JSON
//  (um JSON compacto por mensagem; ver PROTOCOLO.md): conta, simbolos,
//  tick atual, ticks ao vivo (buffer circular, o MT4 nao tem copy_ticks),
//  posicoes, pendentes, historico e ordens (abrir, fechar POR TICKET,
//  modificar, apagar).
//
//  Seguranca (defesa em profundidade; o Python tambem confere):
//   - MaxPosicoes (padrao 1): ordem_abrir e recusada (1011) se ja houver
//     posicoes + pendentes >= MaxPosicoes no simbolo/magic da ordem.
//   - Magics: se preenchido, ordem_abrir/fechar/modificar/apagar so aceitam
//     esses magics (1012); fechar/modificar/apagar conferem o magic do TICKET.
//   - Token (opcional): se preenchido, todo pedido precisa trazer "tk" igual (1010).
//   - ORDENS SO EM CONTA DEMO (IsDemo()). Em conta real todo comando de
//     ordem devolve erro 1006; nao existe parametro para desligar isso.
//   - SomenteLeitura=true recusa qualquer ordem (erro 1007).
//   - Nenhuma ordem e reenviada. Erro e devolvido ao cliente; quem decide
//     e o robo. Respostas de ordens ficam em cache por "id" (idempotencia).
//   - Fechar e SEMPRE por ticket (OrderClose). Nunca ha "ordem oposta".
//
//  Requisitos: MT4 build 600 ou superior; biblioteca mql-zmq (dingmaotu)
//  instalada: MQL4/Include/Zmq/*.mqh, MQL4/Include/Mql/*.mqh e as DLLs
//  libzmq.dll / libsodium.dll em MQL4/Libraries (versao x86 do MT4);
//  "Permitir importacao de DLL" ligado nas opcoes do EA; AutoTrading ligado.
//  CONFERIR: nomes/assinaturas da biblioteca (Context, Socket, ZmqMsg,
//  bind, recv(msg, nowait), send(msg), setLinger) foram escritos de memoria.
//  CONFERIR tambem: constantes SYMBOL_* de CmdSimbolo (SYMBOL_TRADE_EXEMODE,
//  SYMBOL_PATH, SYMBOL_DESCRIPTION, SYMBOL_CURRENCY_*) e StringSplit/StringToInteger.
//
//  Limites: o historico (OrdersHistoryTotal) depende de "Historico da
//  conta > Todo o historico" no terminal; ticks com bid/ask iguais ao
//  ultimo sao fundidos; o tempo e em SEGUNDOS (MODE_TIME) mais
//  GetTickCount() do EA (ms, local, volta a zero a cada ~49 dias).
//+------------------------------------------------------------------+
#property copyright "robowin"
#property version   "1.00"
#property strict

#include <stdlib.mqh>
#include <Zmq/Zmq.mqh>

//--- parametros
input string BindEndereco  = "tcp://127.0.0.1:5560"; // somente 127.0.0.1
input string Simbolos      = "";    // simbolos extras para gravar ticks, separados por virgula, SEM espacos
input bool   SomenteLeitura = false; // true = recusa todas as ordens
input bool   SltpEmSeguida  = false; // true = abre sem SL/TP e aplica em seguida (corretoras ECN/market execution)
input int    TimerMs        = 5;     // intervalo do temporizador em ms
input int    MaxDesvio      = 50;    // teto do desvio (slippage) aceito por ordem, em pontos
input int    MaxPosicoes    = 1;     // maximo de posicoes + pendentes por simbolo/magic (0 = sem limite; NAO recomendado)
input string Magics         = "";    // magics aceitos (ordem_abrir/fechar/modificar/apagar), separados por virgula; vazio = sem filtro
input string Token          = "";    // se preenchido, todo pedido precisa trazer "tk" igual (mesmo valor em [mt5].mt4_token / ROBOWIN_MT4_TOKEN)

//--- constantes do protocolo
#define VERSAO_PROTOCOLO   1
#define EA_VERSAO          "1.00"
#define MAX_SIMB           4
#define CAP                20000     // ticks por simbolo no buffer circular
#define MAX_HIST           500       // itens de historico por resposta
#define CACHE_IDEM         64
#define MAX_FECHADOS       256

#define ERRO_JSON_INVALIDO 1001
#define ERRO_ARGUMENTO     1002
#define ERRO_VERSAO        1003
#define ERRO_SIMBOLO       1004
#define ERRO_TICKET        1005
#define ERRO_CONTA_NAO_DEMO 1006
#define ERRO_SOMENTE_LEITURA 1007
#define ERRO_COMANDO       1008
#define ERRO_SLTP_FECHADA  1009
#define ERRO_TOKEN         1010
#define ERRO_LIMITE        1011
#define ERRO_MAGIC         1012

//--- estado global
Context g_ctx("RobowinBridge");
Socket  g_sock(g_ctx, ZMQ_REP);

long     g_epoca = 0;
datetime g_inicio = 0;

string   g_sym[MAX_SIMB];
int      g_nsym = 0;
long     g_seq[MAX_SIMB];
double   g_ult_bid[MAX_SIMB];
double   g_ult_ask[MAX_SIMB];
long     g_ult_t[MAX_SIMB];

long     r_seq[MAX_SIMB * CAP];
long     r_t[MAX_SIMB * CAP];
long     r_gtc[MAX_SIMB * CAP];
double   r_bid[MAX_SIMB * CAP];
double   r_ask[MAX_SIMB * CAP];

string   g_idem_id[CACHE_IDEM];
string   g_idem_resp[CACHE_IDEM];
int      g_idem_n = 0;

long     g_fechados[MAX_FECHADOS];
int      g_nfech = 0;

//+------------------------------------------------------------------+
//| JSON minimo: so le chaves de objeto compacto (sem espacos)        |
//+------------------------------------------------------------------+
int JFind(const string j, const string key)
  {
   string pat = "\"" + key + "\":";
   int from = 0;
   int tam = StringLen(pat);
   while(true)
     {
      int p = StringFind(j, pat, from);
      if(p < 0)
         return(-1);
      if(p == 0 || StringGetCharacter(j, p - 1) != 92) // 92 = barra invertida
         return(p + tam);
      from = p + 1;
     }
   return(-1);
  }

string JStr(const string j, const string key, const string def = "")
  {
   int p = JFind(j, key);
   if(p < 0)
      return(def);
   if(StringGetCharacter(j, p) != 34) // 34 = aspas
      return(def);
   string out = "";
   int n = StringLen(j);
   for(int i = p + 1; i < n; i++)
     {
      int c = StringGetCharacter(j, i);
      if(c == 92)
        {
         i++;
         if(i >= n)
            break;
         int d = StringGetCharacter(j, i);
         if(d == 117) // \uXXXX: nao suportado, vira '?'
           {
            out += "?";
            i += 4;
           }
         else
            out += CharToString((uchar)d);
         continue;
        }
      if(c == 34)
         break;
      out += CharToString((uchar)c);
     }
   return(out);
  }

double JNum(const string j, const string key, const double def = 0.0)
  {
   int p = JFind(j, key);
   if(p < 0)
      return(def);
   int n = StringLen(j);
   int e = p;
   while(e < n)
     {
      int c = StringGetCharacter(j, e);
      if(c == 44 || c == 125) // virgula ou chave
         break;
      e++;
     }
   string t = StringSubstr(j, p, e - p);
   if(t == "true")
      return(1.0);
   if(t == "false" || t == "null")
      return(0.0);
   return(StringToDouble(t));
  }

string JEsc(const string s)
  {
   string out = "";
   int n = StringLen(s);
   for(int i = 0; i < n; i++)
     {
      int c = StringGetCharacter(s, i);
      if(c == 34 || c == 92)
         out += "\\" + CharToString((uchar)c);
      else if(c < 32 || c > 126)
         out += "?";
      else
         out += CharToString((uchar)c);
     }
   return(out);
  }

string D(const double x, const int dig = 8)
  {
   return(DoubleToString(x, dig));
  }

string L(const long x)
  {
   return(IntegerToString(x));
  }

string B(const bool x)
  {
   return(x ? "true" : "false");
  }

string OkJson(const string id, const string dados)
  {
   return("{\"v\":1,\"id\":\"" + JEsc(id) + "\",\"ok\":true,\"dados\":" + dados + "}");
  }

string ErrJson(const string id, const int codigo, const string msg, const string origem)
  {
   return("{\"v\":1,\"id\":\"" + JEsc(id) + "\",\"ok\":false,\"erro\":{\"codigo\":" + IntegerToString(codigo) +
          ",\"msg\":\"" + JEsc(msg) + "\",\"origem\":\"" + origem + "\"}}");
  }

string ErrMt4(const string id, const int codigo)
  {
   return(ErrJson(id, codigo, ErrorDescription(codigo), "mt4"));
  }

//+------------------------------------------------------------------+
//| Buffer circular de ticks                                          |
//+------------------------------------------------------------------+
int SlotDe(const string sym)
  {
   for(int i = 0; i < g_nsym; i++)
      if(g_sym[i] == sym)
         return(i);
   return(-1);
  }

int AdicionarSlot(const string sym)
  {
   int s = SlotDe(sym);
   if(s >= 0)
      return(s);
   if(g_nsym >= MAX_SIMB || StringLen(sym) == 0)
      return(-1);
   SymbolSelect(sym, true);
   g_sym[g_nsym] = sym;
   g_seq[g_nsym] = 0;
   g_ult_bid[g_nsym] = 0.0;
   g_ult_ask[g_nsym] = 0.0;
   g_ult_t[g_nsym] = 0;
   g_nsym++;
   return(g_nsym - 1);
  }

void GravarTick(const int slot)
  {
   if(slot < 0)
      return;
   string sym = g_sym[slot];
   double bid = MarketInfo(sym, MODE_BID);
   double ask = MarketInfo(sym, MODE_ASK);
   long t = (long)MarketInfo(sym, MODE_TIME);
   if(bid <= 0.0 || ask <= 0.0 || t <= 0)
      return;
   if(bid == g_ult_bid[slot] && ask == g_ult_ask[slot] && t == g_ult_t[slot])
      return; // nada novo
   g_seq[slot]++;
   int idx = slot * CAP + (int)((g_seq[slot] - 1) % CAP);
   r_seq[idx] = g_seq[slot];
   r_t[idx] = t;
   r_gtc[idx] = (long)GetTickCount();
   r_bid[idx] = bid;
   r_ask[idx] = ask;
   g_ult_bid[slot] = bid;
   g_ult_ask[slot] = ask;
   g_ult_t[slot] = t;
  }

void GravarTodos()
  {
   for(int i = 0; i < g_nsym; i++)
      GravarTick(i);
  }

//+------------------------------------------------------------------+
//| Leituras                                                          |
//+------------------------------------------------------------------+
string CmdOla(const string id)
  {
   string d = "{\"versao_protocolo\":" + IntegerToString(VERSAO_PROTOCOLO) +
              ",\"ea\":\"RobowinBridge\",\"ea_versao\":\"" + EA_VERSAO + "\",\"epoca\":" + L(g_epoca) +
              ",\"duble\":false,\"build\":" + IntegerToString(TerminalInfoInteger(TERMINAL_BUILD)) +
              ",\"caminho\":\"" + JEsc(TerminalInfoString(TERMINAL_PATH)) + "\"" +
              ",\"caminho_dados\":\"" + JEsc(TerminalInfoString(TERMINAL_DATA_PATH)) + "\"" +
              ",\"conta_demo\":" + B(IsDemo()) + ",\"somente_leitura\":" + B(SomenteLeitura) + "}";
   return(OkJson(id, d));
  }

string CmdConta(const string id)
  {
   string d = "{\"login\":" + IntegerToString(AccountNumber()) +
              ",\"servidor\":\"" + JEsc(AccountServer()) + "\"" +
              ",\"empresa\":\"" + JEsc(AccountCompany()) + "\"" +
              ",\"moeda\":\"" + JEsc(AccountCurrency()) + "\"" +
              ",\"nome\":\"" + JEsc(AccountName()) + "\"" +
              ",\"demo\":" + B(IsDemo()) +
              ",\"trade_allowed\":" + B(IsTradeAllowed()) +
              ",\"expert_enabled\":" + B(IsExpertEnabled()) +
              ",\"conectado\":" + B(IsConnected()) +
              ",\"dlls_permitidas\":" + B(IsDllsAllowed()) +
              ",\"saldo\":" + D(AccountBalance(), 2) + ",\"patrimonio\":" + D(AccountEquity(), 2) +
              ",\"lucro\":" + D(AccountProfit(), 2) + ",\"margem\":" + D(AccountMargin(), 2) +
              ",\"margem_livre\":" + D(AccountFreeMargin(), 2) + ",\"alavancagem\":" + IntegerToString(AccountLeverage()) +
              ",\"hedging\":true,\"t_servidor_s\":" + L((long)TimeCurrent()) + ",\"gtc\":" + L((long)GetTickCount()) + "}";
   return(OkJson(id, d));
  }

string CmdSimbolos(const string id)
  {
   int n = SymbolsTotal(false);
   string lista = "";
   for(int i = 0; i < n; i++)
     {
      if(i > 0)
         lista += ",";
      lista += "\"" + JEsc(SymbolName(i, false)) + "\"";
     }
   return(OkJson(id, "{\"simbolos\":[" + lista + "]}"));
  }

bool SimboloExiste(const string sym)
  {
   if(StringLen(sym) == 0)
      return(false);
   ResetLastError();
   double pt = MarketInfo(sym, MODE_POINT);
   return(pt > 0.0 && GetLastError() != 4106);
  }

string CmdSimbolo(const string id, const string req)
  {
   string sym = JStr(req, "simbolo");
   if(!SimboloExiste(sym))
      return(ErrJson(id, ERRO_SIMBOLO, "simbolo desconhecido: " + sym, "ponte"));
   long exe = SymbolInfoInteger(sym, SYMBOL_TRADE_EXEMODE);
   string d = "{\"nome\":\"" + JEsc(sym) + "\"" +
              ",\"caminho\":\"" + JEsc(SymbolInfoString(sym, SYMBOL_PATH)) + "\"" +
              ",\"descricao\":\"" + JEsc(SymbolInfoString(sym, SYMBOL_DESCRIPTION)) + "\"" +
              ",\"digits\":" + IntegerToString((int)MarketInfo(sym, MODE_DIGITS)) +
              ",\"ponto\":" + D(MarketInfo(sym, MODE_POINT)) +
              ",\"visivel\":" + B(SymbolInfoInteger(sym, SYMBOL_SELECT) != 0) +
              ",\"spread\":" + IntegerToString((int)MarketInfo(sym, MODE_SPREAD)) +
              ",\"negociavel\":" + B(MarketInfo(sym, MODE_TRADEALLOWED) != 0.0) +
              ",\"execucao\":" + L(exe) +
              ",\"tick_size\":" + D(MarketInfo(sym, MODE_TICKSIZE)) +
              ",\"tick_value\":" + D(MarketInfo(sym, MODE_TICKVALUE)) +
              ",\"lot_size\":" + D(MarketInfo(sym, MODE_LOTSIZE)) +
              ",\"lote_min\":" + D(MarketInfo(sym, MODE_MINLOT)) +
              ",\"lote_max\":" + D(MarketInfo(sym, MODE_MAXLOT)) +
              ",\"lote_passo\":" + D(MarketInfo(sym, MODE_LOTSTEP)) +
              ",\"stop_level\":" + IntegerToString((int)MarketInfo(sym, MODE_STOPLEVEL)) +
              ",\"freeze_level\":" + IntegerToString((int)MarketInfo(sym, MODE_FREEZELEVEL)) +
              ",\"margem_requerida\":" + D(MarketInfo(sym, MODE_MARGINREQUIRED)) +
              ",\"expiracao_s\":" + L((long)MarketInfo(sym, MODE_EXPIRATION)) +
              ",\"moeda_lucro\":\"" + JEsc(SymbolInfoString(sym, SYMBOL_CURRENCY_PROFIT)) + "\"" +
              ",\"moeda_base\":\"" + JEsc(SymbolInfoString(sym, SYMBOL_CURRENCY_BASE)) + "\"}";
   return(OkJson(id, d));
  }

string CmdSelecionar(const string id, const string req)
  {
   string sym = JStr(req, "simbolo");
   bool ativar = (JNum(req, "ativar", 1.0) != 0.0);
   if(!SimboloExiste(sym))
      return(OkJson(id, "{\"ok\":false}"));
   bool r = SymbolSelect(sym, ativar);
   return(OkJson(id, "{\"ok\":" + B(r) + "}"));
  }

string CmdTick(const string id, const string req)
  {
   string sym = JStr(req, "simbolo");
   if(!SimboloExiste(sym))
      return(ErrJson(id, ERRO_SIMBOLO, "simbolo desconhecido: " + sym, "ponte"));
   int slot = SlotDe(sym);
   long seq = 0;
   if(slot >= 0)
     {
      GravarTick(slot);
      seq = g_seq[slot];
     }
   long t = (long)MarketInfo(sym, MODE_TIME);
   string d = "{\"t_s\":" + L(t) + ",\"t_corrente_s\":" + L((long)TimeCurrent()) + ",\"gtc\":" + L((long)GetTickCount()) +
              ",\"bid\":" + D(MarketInfo(sym, MODE_BID)) + ",\"ask\":" + D(MarketInfo(sym, MODE_ASK)) +
              ",\"seq\":" + L(seq) + "}";
   return(OkJson(id, d));
  }

string CmdTicks(const string id, const string req)
  {
   string sym = JStr(req, "simbolo");
   int slot = SlotDe(sym);
   if(slot < 0)
      return(ErrJson(id, ERRO_SIMBOLO, "sem buffer de ticks para " + sym + " (anexe o EA ao grafico ou liste em Simbolos)", "ponte"));
   GravarTick(slot);
   long desde = (long)JNum(req, "desde_seq", -1.0);
   long epoca_cli = (long)JNum(req, "epoca", 0.0);
   int mx = (int)JNum(req, "max", 5000.0);
   if(mx < 1)
      mx = 1;
   if(mx > 5000)
      mx = 5000;
   bool reiniciou = (epoca_cli != 0 && epoca_cli != g_epoca);
   if(reiniciou)
      desde = -1;
   long ultimo = g_seq[slot];
   long mais_antigo = ultimo - CAP + 1;
   if(mais_antigo < 1)
      mais_antigo = 1;
   bool perdeu = (desde >= 0 && ultimo > 0 && mais_antigo > desde + 1);
   long ini = desde + 1;
   if(ini < mais_antigo)
      ini = mais_antigo;
   long fim = ultimo;
   bool mais = false;
   if(fim - ini + 1 > mx)
     {
      fim = ini + mx - 1;
      mais = true;
     }
   string linhas = "";
   for(long s = ini; s <= fim; s++)
     {
      int idx = slot * CAP + (int)((s - 1) % CAP);
      if(r_seq[idx] != s)
         continue; // sobrescrito: nao deve acontecer
      if(StringLen(linhas) > 0)
         linhas += ",";
      linhas += "[" + L(s) + "," + L(r_t[idx]) + "," + L(r_gtc[idx]) + "," + D(r_bid[idx]) + "," + D(r_ask[idx]) + ",0]";
     }
   string d = "{\"epoca\":" + L(g_epoca) + ",\"ticks\":[" + linhas + "],\"seq_max\":" + L(ultimo) +
              ",\"perdeu\":" + B(perdeu) + ",\"reiniciou\":" + B(reiniciou) + ",\"mais\":" + B(mais) + "}";
   return(OkJson(id, d));
  }

//--- item de ordem aberta/pendente (a ordem ja deve estar selecionada)
string ItemOrdemSelecionada()
  {
   string sym = OrderSymbol();
   int tipo = OrderType();
   double atual = 0.0;
   if(tipo == OP_BUY || tipo == OP_BUYLIMIT || tipo == OP_BUYSTOP)
      atual = MarketInfo(sym, MODE_BID);
   else
      atual = MarketInfo(sym, MODE_ASK);
   return("{\"ticket\":" + IntegerToString(OrderTicket()) + ",\"simbolo\":\"" + JEsc(sym) + "\"" +
          ",\"tipo\":" + IntegerToString(tipo) + ",\"volume\":" + D(OrderLots()) +
          ",\"p_abertura\":" + D(OrderOpenPrice()) + ",\"sl\":" + D(OrderStopLoss()) + ",\"tp\":" + D(OrderTakeProfit()) +
          ",\"magic\":" + IntegerToString(OrderMagicNumber()) + ",\"comentario\":\"" + JEsc(OrderComment()) + "\"" +
          ",\"t_abertura_s\":" + L((long)OrderOpenTime()) + ",\"comissao\":" + D(OrderCommission(), 2) +
          ",\"swap\":" + D(OrderSwap(), 2) + ",\"lucro\":" + D(OrderProfit(), 2) + ",\"p_atual\":" + D(atual) + "}");
  }

string CmdPosicoes(const string id, const string req, const bool pendentes)
  {
   string filtro = JStr(req, "simbolo");
   string lista = "";
   int n = OrdersTotal();
   for(int i = 0; i < n; i++)
     {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;
      int tipo = OrderType();
      bool eh_pendente = (tipo >= OP_BUYLIMIT);
      if(eh_pendente != pendentes)
         continue;
      if(StringLen(filtro) > 0 && OrderSymbol() != filtro)
         continue;
      if(tipo != OP_BUY && tipo != OP_SELL && !eh_pendente)
         continue;
      if(StringLen(lista) > 0)
         lista += ",";
      lista += ItemOrdemSelecionada();
     }
   if(pendentes)
      return(OkJson(id, "{\"ordens\":[" + lista + "]}"));
   return(OkJson(id, "{\"posicoes\":[" + lista + "]}"));
  }

bool FoiFechadoPeloEA(const long ticket)
  {
   for(int i = 0; i < MAX_FECHADOS && i < g_nfech; i++)
      if(g_fechados[i] == ticket)
         return(true);
   return(false);
  }

void RegistrarFechado(const long ticket)
  {
   g_fechados[g_nfech % MAX_FECHADOS] = ticket;
   g_nfech++;
  }

string CmdHistorico(const string id, const string req)
  {
   long de_s = (long)JNum(req, "de_s", 0.0);
   long ate_s = (long)JNum(req, "ate_s", 2000000000.0);
   string lista = "";
   int achados = 0;
   int n = OrdersHistoryTotal();
   for(int i = n - 1; i >= 0 && achados < MAX_HIST; i--)
     {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_HISTORY))
         continue;
      int tipo = OrderType();
      if(tipo > OP_SELLSTOP)
         continue; // saldo/credito
      long t_fe = (long)OrderCloseTime();
      long t_ab = (long)OrderOpenTime();
      bool dentro = ((t_fe >= de_s && t_fe <= ate_s) || (t_ab >= de_s && t_ab <= ate_s));
      if(!dentro)
         continue;
      int fechado_ea = -1;
      if(FoiFechadoPeloEA(OrderTicket()))
         fechado_ea = 1;
      else if(OrderCloseTime() >= g_inicio)
         fechado_ea = 0;
      if(achados > 0)
         lista += ",";
      lista += "{\"ticket\":" + IntegerToString(OrderTicket()) + ",\"tipo\":" + IntegerToString(tipo) +
               ",\"simbolo\":\"" + JEsc(OrderSymbol()) + "\",\"volume\":" + D(OrderLots()) +
               ",\"p_abertura\":" + D(OrderOpenPrice()) + ",\"p_fechamento\":" + D(OrderClosePrice()) +
               ",\"t_abertura_s\":" + L(t_ab) + ",\"t_fechamento_s\":" + L(t_fe) +
               ",\"sl\":" + D(OrderStopLoss()) + ",\"tp\":" + D(OrderTakeProfit()) +
               ",\"lucro\":" + D(OrderProfit(), 2) + ",\"comissao\":" + D(OrderCommission(), 2) +
               ",\"swap\":" + D(OrderSwap(), 2) + ",\"magic\":" + IntegerToString(OrderMagicNumber()) +
               ",\"comentario\":\"" + JEsc(OrderComment()) + "\",\"fechado_ea\":" + IntegerToString(fechado_ea) + "}";
      achados++;
     }
   return(OkJson(id, "{\"itens\":[" + lista + "]}"));
  }

//+------------------------------------------------------------------+
//| Ordens                                                            |
//+------------------------------------------------------------------+
//--- devolve "" se a conta/EA permite operar; senao o JSON de erro
string ChecarPodeOperar(const string id)
  {
   if(!IsDemo())
      return(ErrJson(id, ERRO_CONTA_NAO_DEMO, "conta REAL: a ponte so negocia em conta DEMO", "ponte"));
   if(SomenteLeitura)
      return(ErrJson(id, ERRO_SOMENTE_LEITURA, "EA em modo SomenteLeitura", "ponte"));
   if(!IsExpertEnabled() || !IsTradeAllowed())
      return(ErrMt4(id, 4109)); // ERR_TRADE_NOT_ALLOWED
   return("");
  }

//--- defesa em profundidade: magic aceito? (Magics vazio = sem filtro)
bool MagicPermitido(const int magic)
  {
   if(StringLen(Magics) == 0)
      return(true);
   string partes[];
   int n = StringSplit(Magics, ',', partes);
   for(int i = 0; i < n; i++)
      if((int)StringToInteger(partes[i]) == magic)
         return(true);
   return(false);
  }

//--- posicoes abertas + pendentes do simbolo/magic
int ContarSimboloMagic(const string sym, const int magic)
  {
   int c = 0;
   int n = OrdersTotal();
   for(int i = 0; i < n; i++)
     {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;
      if(OrderSymbol() == sym && OrderMagicNumber() == magic)
         c++;
     }
   return(c);
  }

//--- ticket ja selecionado (OrderSelect): devolve "" se o magic dele e aceito; senao o JSON de erro
string ChecarMagicDoTicket(const string id)
  {
   if(!MagicPermitido(OrderMagicNumber()))
      return(ErrJson(id, ERRO_MAGIC, "ticket de magic " + IntegerToString(OrderMagicNumber()) + " fora de Magics", "ponte"));
   return("");
  }

double PrecoMercado(const string sym, const int tipo)
  {
   RefreshRates();
   if(tipo == OP_BUY)
      return(MarketInfo(sym, MODE_ASK));
   return(MarketInfo(sym, MODE_BID));
  }

string CmdOrdemAbrir(const string id, const string req)
  {
   string sym = JStr(req, "simbolo");
   if(!SimboloExiste(sym))
      return(ErrJson(id, ERRO_SIMBOLO, "simbolo desconhecido: " + sym, "ponte"));
   int tipo = (int)JNum(req, "tipo", -1.0);
   if(tipo < OP_BUY || tipo > OP_SELLLIMIT)
      return(ErrJson(id, ERRO_ARGUMENTO, "tipo de ordem nao suportado (0..3)", "ponte"));
   double vol = JNum(req, "volume", 0.0);
   if(vol <= 0.0)
      return(ErrJson(id, ERRO_ARGUMENTO, "volume invalido", "ponte"));
   int dig = (int)MarketInfo(sym, MODE_DIGITS);
   double preco = JNum(req, "preco", 0.0);
   double sl = NormalizeDouble(JNum(req, "sl", 0.0), dig);
   double tp = NormalizeDouble(JNum(req, "tp", 0.0), dig);
   int desvio = (int)JNum(req, "desvio", 0.0);
   if(desvio < 0)
      desvio = 0;
   if(desvio > MaxDesvio)
      desvio = MaxDesvio;
   int magic = (int)JNum(req, "magic", 0.0);
   if(!MagicPermitido(magic))
      return(ErrJson(id, ERRO_MAGIC, "magic " + IntegerToString(magic) + " fora de Magics", "ponte"));
   if(MaxPosicoes > 0 && ContarSimboloMagic(sym, magic) >= MaxPosicoes)
      return(ErrJson(id, ERRO_LIMITE, "posicoes+pendentes de " + sym + "/magic " + IntegerToString(magic) + " ja no maximo (" +
                     IntegerToString(MaxPosicoes) + ")", "ponte"));
   string com = JStr(req, "comentario");
   datetime expira = 0;
   if(tipo >= OP_BUYLIMIT)
     {
      if(preco <= 0.0)
         return(ErrJson(id, ERRO_ARGUMENTO, "ordem pendente exige preco", "ponte"));
      if(JStr(req, "validade") == "dia")
         expira = (datetime)((((long)TimeCurrent()) / 86400 + 1) * 86400 - 1); // fim do dia do servidor
     }
   else
      preco = PrecoMercado(sym, tipo); // a mercado: sempre o preco ATUAL (o do cliente pode estar velho)
   preco = NormalizeDouble(preco, dig);
   if(IsTradeContextBusy())
      return(ErrMt4(id, 146));
   double sl_envio = sl;
   double tp_envio = tp;
   if(SltpEmSeguida && tipo <= OP_SELL)
     {
      sl_envio = 0.0;
      tp_envio = 0.0;
     }
   ResetLastError();
   int ticket = OrderSend(sym, tipo, vol, preco, desvio, sl_envio, tp_envio, com, magic, expira, clrNONE);
   if(ticket < 0)
      return(ErrMt4(id, GetLastError()));
   double p_exec = preco;
   long t_s = (long)TimeCurrent();
   if(OrderSelect(ticket, SELECT_BY_TICKET))
     {
      p_exec = OrderOpenPrice();
      t_s = (long)OrderOpenTime();
     }
   if(SltpEmSeguida && tipo <= OP_SELL && (sl > 0.0 || tp > 0.0))
     {
      ResetLastError();
      bool ok = OrderModify(ticket, p_exec, sl, tp, 0, clrNONE); // p_exec (nao OrderOpenPrice): a selecao pode ter falhado
      int e = GetLastError();
      if(!ok && e != 1) // 1 = sem mudancas
        {
         // SL/TP nao foi aceito: NAO deixa posicao sem stop. Fecha por ticket e avisa.
         ResetLastError();
         bool fechou = OrderClose(ticket, vol, PrecoMercado(sym, (tipo == OP_BUY) ? OP_SELL : OP_BUY), MaxDesvio, clrNONE);
         if(fechou)
            RegistrarFechado(ticket);
         return(ErrJson(id, ERRO_SLTP_FECHADA, "SL/TP recusado apos abrir (erro " + IntegerToString(e) + "); posicao " +
                        (fechou ? "fechada por seguranca" : "NAO PUDE FECHAR - verifique manualmente"), "ponte"));
        }
     }
   return(OkJson(id, "{\"ticket\":" + IntegerToString(ticket) + ",\"preco\":" + D(p_exec) + ",\"volume\":" + D(vol) +
                 ",\"t_s\":" + L(t_s) + "}"));
  }

//--- procura o ticket do restante apos fechamento parcial (comentario "from #<ticket>")
int TicketRestante(const int ticket_orig)
  {
   string alvo = "from #" + IntegerToString(ticket_orig);
   int n = OrdersTotal();
   for(int i = 0; i < n; i++)
     {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;
      if(OrderComment() == alvo)
         return(OrderTicket());
     }
   return(0);
  }

string CmdOrdemFechar(const string id, const string req)
  {
   int ticket = (int)JNum(req, "ticket", 0.0);
   if(ticket <= 0 || !OrderSelect(ticket, SELECT_BY_TICKET, MODE_TRADES))
      return(ErrJson(id, 4108, "ticket " + IntegerToString(ticket) + " inexistente", "mt4"));
   int tipo = OrderType();
   if((tipo != OP_BUY && tipo != OP_SELL) || OrderCloseTime() != 0)
      return(ErrJson(id, 4108, "ticket " + IntegerToString(ticket) + " nao e uma posicao aberta", "mt4"));
   string rm = ChecarMagicDoTicket(id);
   if(StringLen(rm) > 0)
      return(rm);
   string sym = OrderSymbol();
   double vol = JNum(req, "volume", 0.0);
   double aberto = OrderLots();
   if(vol <= 0.0 || vol > aberto)
      vol = aberto;
   int desvio = (int)JNum(req, "desvio", 0.0);
   if(desvio < 0)
      desvio = 0;
   if(desvio > MaxDesvio)
      desvio = MaxDesvio;
   if(IsTradeContextBusy())
      return(ErrMt4(id, 146));
   double preco = PrecoMercado(sym, (tipo == OP_BUY) ? OP_SELL : OP_BUY); // fecha no lado contrario do book
   preco = NormalizeDouble(preco, (int)MarketInfo(sym, MODE_DIGITS));
   ResetLastError();
   bool ok = OrderClose(ticket, vol, preco, desvio, clrNONE); // SEMPRE por ticket
   if(!ok)
      return(ErrMt4(id, GetLastError()));
   RegistrarFechado(ticket);
   int resto = 0;
   if(vol < aberto)
      resto = TicketRestante(ticket);
   return(OkJson(id, "{\"ticket\":" + IntegerToString(ticket) + ",\"preco\":" + D(preco) + ",\"volume_fechado\":" + D(vol) +
                 ",\"ticket_restante\":" + IntegerToString(resto) + "}"));
  }

string CmdOrdemModificar(const string id, const string req)
  {
   int ticket = (int)JNum(req, "ticket", 0.0);
   if(ticket <= 0 || !OrderSelect(ticket, SELECT_BY_TICKET, MODE_TRADES))
      return(ErrJson(id, 4108, "ticket " + IntegerToString(ticket) + " inexistente", "mt4"));
   if(OrderCloseTime() != 0)
      return(ErrJson(id, 4108, "ticket " + IntegerToString(ticket) + " ja encerrado", "mt4"));
   string rm = ChecarMagicDoTicket(id);
   if(StringLen(rm) > 0)
      return(rm);
   int dig = (int)MarketInfo(OrderSymbol(), MODE_DIGITS);
   double sl = NormalizeDouble(JNum(req, "sl", 0.0), dig);
   double tp = NormalizeDouble(JNum(req, "tp", 0.0), dig);
   double preco = OrderOpenPrice();
   if(OrderType() >= OP_BUYLIMIT && JNum(req, "preco", 0.0) > 0.0)
      preco = NormalizeDouble(JNum(req, "preco", 0.0), dig);
   if(IsTradeContextBusy())
      return(ErrMt4(id, 146));
   ResetLastError();
   bool ok = OrderModify(ticket, preco, sl, tp, OrderExpiration(), clrNONE);
   int e = GetLastError();
   if(!ok && e != 1) // 1 = nada mudou: tratado como sucesso
      return(ErrMt4(id, e));
   return(OkJson(id, "{\"ticket\":" + IntegerToString(ticket) + "}"));
  }

string CmdOrdemApagar(const string id, const string req)
  {
   int ticket = (int)JNum(req, "ticket", 0.0);
   if(ticket <= 0 || !OrderSelect(ticket, SELECT_BY_TICKET, MODE_TRADES))
      return(ErrJson(id, 4108, "ticket " + IntegerToString(ticket) + " inexistente", "mt4"));
   if(OrderType() < OP_BUYLIMIT || OrderCloseTime() != 0)
      return(ErrJson(id, 4108, "ticket " + IntegerToString(ticket) + " nao e ordem pendente", "mt4"));
   string rm = ChecarMagicDoTicket(id);
   if(StringLen(rm) > 0)
      return(rm);
   if(IsTradeContextBusy())
      return(ErrMt4(id, 146));
   ResetLastError();
   if(!OrderDelete(ticket, clrNONE))
      return(ErrMt4(id, GetLastError()));
   return(OkJson(id, "{\"ticket\":" + IntegerToString(ticket) + "}"));
  }

//+------------------------------------------------------------------+
//| Despacho                                                          |
//+------------------------------------------------------------------+
bool ComandoDeOrdem(const string cmd)
  {
   return(cmd == "ordem_abrir" || cmd == "ordem_fechar" || cmd == "ordem_modificar" || cmd == "ordem_apagar");
  }

string CacheBuscar(const string id)
  {
   for(int i = 0; i < CACHE_IDEM; i++)
      if(g_idem_id[i] == id && StringLen(id) > 0)
         return(g_idem_resp[i]);
   return("");
  }

void CacheGuardar(const string id, const string resp)
  {
   int i = g_idem_n % CACHE_IDEM;
   g_idem_id[i] = id;
   g_idem_resp[i] = resp;
   g_idem_n++;
  }

string Tratar(const string req)
  {
   string id = JStr(req, "id");
   if(StringLen(id) == 0 || StringGetCharacter(req, 0) != 123) // 123 = '{'
      return(ErrJson(id, ERRO_JSON_INVALIDO, "JSON invalido ou sem id", "ponte"));
   if((int)JNum(req, "v", 0.0) != VERSAO_PROTOCOLO)
      return(ErrJson(id, ERRO_VERSAO, "versao do protocolo nao suportada (esperado 1)", "ponte"));
   if(StringLen(Token) > 0 && JStr(req, "tk") != Token) // token opcional: confere ANTES de cache/leitura/ordem
      return(ErrJson(id, ERRO_TOKEN, "token ausente ou invalido", "ponte"));
   string cmd = JStr(req, "cmd");
   if(ComandoDeOrdem(cmd))
     {
      string guardada = CacheBuscar(id);
      if(StringLen(guardada) > 0)
         return(guardada); // idempotencia: mesmo id => mesma resposta, nada e reexecutado
      string bloqueio = ChecarPodeOperar(id);
      if(StringLen(bloqueio) > 0)
         return(bloqueio);
      string r = "";
      if(cmd == "ordem_abrir")
         r = CmdOrdemAbrir(id, req);
      else if(cmd == "ordem_fechar")
         r = CmdOrdemFechar(id, req);
      else if(cmd == "ordem_modificar")
         r = CmdOrdemModificar(id, req);
      else
         r = CmdOrdemApagar(id, req);
      CacheGuardar(id, r);
      return(r);
     }
   if(cmd == "ola")
      return(CmdOla(id));
   if(cmd == "conta")
      return(CmdConta(id));
   if(cmd == "simbolos")
      return(CmdSimbolos(id));
   if(cmd == "simbolo")
      return(CmdSimbolo(id, req));
   if(cmd == "selecionar")
      return(CmdSelecionar(id, req));
   if(cmd == "tick")
      return(CmdTick(id, req));
   if(cmd == "ticks")
      return(CmdTicks(id, req));
   if(cmd == "posicoes")
      return(CmdPosicoes(id, req, false));
   if(cmd == "pendentes")
      return(CmdPosicoes(id, req, true));
   if(cmd == "historico")
      return(CmdHistorico(id, req));
   return(ErrJson(id, ERRO_COMANDO, "comando desconhecido: " + cmd, "ponte"));
  }

//--- atende ate N pedidos pendentes sem bloquear (REP: sempre responde apos receber)
void AtenderPedidos()
  {
   for(int k = 0; k < 8; k++)
     {
      ZmqMsg pedido;
      if(!g_sock.recv(pedido, true)) // CONFERIR: recv(msg, nowait)
         return;
      string req = pedido.getData();
      string resp = Tratar(req);
      ZmqMsg resposta(resp);
      g_sock.send(resposta); // CONFERIR: send(msg)
     }
  }

//+------------------------------------------------------------------+
//| Eventos                                                           |
//+------------------------------------------------------------------+
int OnInit()
  {
   g_inicio = TimeCurrent();
   g_epoca = ((long)TimeLocal()) * 1000 + (long)(GetTickCount() % 1000) + 1;
   AdicionarSlot(Symbol());
   if(StringLen(Simbolos) > 0)
     {
      string partes[];
      int n = StringSplit(Simbolos, ',', partes);
      for(int i = 0; i < n; i++)
         AdicionarSlot(partes[i]);
     }
   if(StringFind(BindEndereco, "tcp://127.0.0.1") != 0 && StringFind(BindEndereco, "ipc://") != 0)
     {
      Print("RobowinBridge: BindEndereco deve ser 127.0.0.1 (ou ipc://). Recusado: ", BindEndereco);
      return(INIT_FAILED);
     }
   g_sock.setLinger(0); // CONFERIR: setLinger
   if(!g_sock.bind(BindEndereco))
     {
      Print("RobowinBridge: falha no bind em ", BindEndereco, " (porta em uso? DLL permitida?)");
      return(INIT_FAILED);
     }
   if(!IsDemo())
      Print("RobowinBridge: conta REAL detectada. Leituras funcionam; TODAS as ordens serao recusadas (erro 1006).");
   if(!EventSetMillisecondTimer(TimerMs))
     {
      Print("RobowinBridge: nao foi possivel criar o temporizador");
      return(INIT_FAILED);
     }
   Print("RobowinBridge ", EA_VERSAO, " ouvindo em ", BindEndereco, " (somente_leitura=", SomenteLeitura, ")");
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   // o socket/contexto sao destruidos com as variaveis globais
  }

void OnTick()
  {
   GravarTick(SlotDe(Symbol()));
  }

void OnTimer()
  {
   GravarTodos();
   AtenderPedidos();
  }
//+------------------------------------------------------------------+

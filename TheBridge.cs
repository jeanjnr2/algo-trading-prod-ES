// TheBridge.cs
//
// Reusable NinjaTrader side of a ZeroMQ signal bridge.
// Python sends only LONG/SHORT signals through ZeroMQ.
// NinjaTrader owns the chart instrument, account, quantity, spread check,
// market entry, TP/SL and protected stop.

#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Globalization;
using System.Linq;
using System.Threading;
using NetMQ;
using NetMQ.Sockets;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
    [TypeConverter(typeof(TheBridgePropertyConverter))]
    public class TheBridge : Strategy
    {
        private const double EsMarginPerContract = 500.0;
        private const int ProfitTargetTicks = 15;
        private const int StopLossTicks = 13;
        private const int ProtectedStopTriggerTicks = 12;
        private const int ProtectedStopTicks = 1;
        private const int MinContracts = 1;
        private const int MaxSpreadTicks = 1;

        private SubscriberSocket signalSub;
        private PushSocket statePush;
        private Thread signalThread;
        private volatile bool keepRunning;
        private volatile bool orderPending;
        private string activeSignalId = string.Empty;
        private double entryAveragePrice;
        private bool protectedStopMoved;
        private DateTime lastPythonHeartbeatUtc = DateTime.MinValue;
        private DateTime lastNinjaHeartbeatUtc = DateTime.MinValue;
        private bool pythonReady;
        private bool pythonConnectionLostLogged;

        [NinjaScriptProperty]
        [Range(1, 65535)]
        [Display(Name = "Signal port", Order = 1, GroupName = "ZeroMQ")]
        public int SignalPort { get; set; }

        [NinjaScriptProperty]
        [Range(1, 65535)]
        [Display(Name = "State port", Order = 2, GroupName = "ZeroMQ")]
        public int StatePort { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Contracts", Order = 1, GroupName = "Risk")]
        public int Contracts { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Enable Auto Sizing", Order = 2, GroupName = "Risk")]
        public bool EnableAutoSizing { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, 10.0)]
        [Display(Name = "Sizing R Multiple", Order = 3, GroupName = "Risk")]
        public double SizingRiskMultiple { get; set; }

        [NinjaScriptProperty]
        [Range(1, 100)]
        [Display(Name = "Max Contracts", Order = 4, GroupName = "Risk")]
        public int MaxContracts { get; set; }

        [NinjaScriptProperty]
        [Range(1, 60)]
        [Display(Name = "Heartbeat interval seconds", Order = 3, GroupName = "ZeroMQ")]
        public int HeartbeatIntervalSeconds { get; set; }

        [NinjaScriptProperty]
        [Range(3, 300)]
        [Display(Name = "Connection timeout seconds", Order = 4, GroupName = "ZeroMQ")]
        public int ConnectionTimeoutSeconds { get; set; }

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "TheBridge";
                Calculate = Calculate.OnEachTick;
                EntriesPerDirection = 1;
                EntryHandling = EntryHandling.AllEntries;
                IsExitOnSessionCloseStrategy = true;
                ExitOnSessionCloseSeconds = 30;
                IsInstantiatedOnEachOptimizationIteration = false;

                SignalPort = 5555;
                StatePort = 5556;
                Contracts = 1;
                EnableAutoSizing = true;
                SizingRiskMultiple = 1.25;
                MaxContracts = 100;
                HeartbeatIntervalSeconds = 2;
                ConnectionTimeoutSeconds = 8;
            }
            else if (State == State.Configure)
            {
                SetProfitTarget(CalculationMode.Ticks, ProfitTargetTicks);
                SetStopLoss(CalculationMode.Ticks, StopLossTicks);
            }
            else if (State == State.Realtime)
            {
                StartZeroMq();
            }
            else if (State == State.Terminated)
            {
                StopZeroMq();
            }
        }

        protected override void OnMarketData(MarketDataEventArgs marketDataUpdate)
        {
            if (State != State.Realtime || marketDataUpdate.MarketDataType != MarketDataType.Last)
                return;

            if (Position.MarketPosition == MarketPosition.Flat || protectedStopMoved)
                return;

            if (entryAveragePrice <= 0)
                return;

            if (Position.MarketPosition == MarketPosition.Long)
            {
                double triggerPrice = entryAveragePrice + ProtectedStopTriggerTicks * TickSize;
                if (marketDataUpdate.Price >= triggerPrice)
                    MoveStopToProtectedPrice(entryAveragePrice + ProtectedStopTicks * TickSize);
            }
            else if (Position.MarketPosition == MarketPosition.Short)
            {
                double triggerPrice = entryAveragePrice - ProtectedStopTriggerTicks * TickSize;
                if (marketDataUpdate.Price <= triggerPrice)
                    MoveStopToProtectedPrice(entryAveragePrice - ProtectedStopTicks * TickSize);
            }
        }

        protected override void OnExecutionUpdate(
            Execution execution,
            string executionId,
            double price,
            int quantity,
            MarketPosition marketPosition,
            string orderId,
            DateTime time)
        {
            if (execution == null || execution.Order == null)
                return;

            if (execution.Order.OrderState != OrderState.Filled && execution.Order.OrderState != OrderState.PartFilled)
                return;

            string orderName = execution.Order.Name ?? string.Empty;
            if (orderName.StartsWith("CE-Long", StringComparison.OrdinalIgnoreCase)
                || orderName.StartsWith("CE-Short", StringComparison.OrdinalIgnoreCase))
            {
                entryAveragePrice = execution.Order.AverageFillPrice;
                protectedStopMoved = false;
                orderPending = false;
                SendState("POSITION_OPEN", activeSignalId, "entry_filled");
                Print("POSITION_OPEN | signal_id=" + activeSignalId + " | avg=" + entryAveragePrice.ToString(CultureInfo.InvariantCulture));
            }
        }

        protected override void OnPositionUpdate(
            Position position,
            double averagePrice,
            int quantity,
            MarketPosition marketPosition)
        {
            if (position == null || position.Account != Account)
                return;

            if (marketPosition == MarketPosition.Flat && !string.IsNullOrEmpty(activeSignalId))
            {
                SendState("POSITION_FLAT", activeSignalId, "flat");
                Print("POSITION_FLAT | signal_id=" + activeSignalId);
                activeSignalId = string.Empty;
                entryAveragePrice = 0;
                protectedStopMoved = false;
                orderPending = false;
            }
        }

        private void StartZeroMq()
        {
            if (signalThread != null)
                return;

            keepRunning = true;
            signalSub = new SubscriberSocket();
            signalSub.Connect(SignalEndpoint);
            signalSub.SubscribeToAnyTopic();

            statePush = new PushSocket();
            statePush.Connect(StateEndpoint);

            signalThread = new Thread(SignalLoop);
            signalThread.IsBackground = true;
            signalThread.Name = "TheBridge-ZeroMQ";
            signalThread.Start();

            Print("ZMQ_STARTED | signal=" + SignalEndpoint + " | state=" + StateEndpoint);
            SendState("READY", string.Empty, "ninja_zmq_started");
            Print("NINJA_READY_SENT | state=" + StateEndpoint);
            PublishPositionSnapshot("startup_snapshot");
        }

        private void StopZeroMq()
        {
            keepRunning = false;

            if (signalThread != null && !signalThread.Join(2000))
                Print("ZMQ_THREAD_STOP_TIMEOUT");

            signalThread = null;

            if (signalSub != null)
            {
                signalSub.Close();
                signalSub.Dispose();
                signalSub = null;
            }

            if (statePush != null)
            {
                statePush.Close();
                statePush.Dispose();
                statePush = null;
            }

            NetMQConfig.Cleanup(false);
            Print("ZMQ_STOPPED");
        }

        private void PublishPositionSnapshot(string reason)
        {
            MarketPosition marketPosition = MarketPosition.Flat;
            int quantity = 0;
            double averagePrice = 0;

            try
            {
                if (Account != null && Instrument != null)
                {
                    foreach (Position accountPosition in Account.Positions)
                    {
                        if (accountPosition != null && accountPosition.Instrument == Instrument)
                        {
                            marketPosition = accountPosition.MarketPosition;
                            quantity = accountPosition.Quantity;
                            averagePrice = accountPosition.AveragePrice;
                            break;
                        }
                    }
                }
            }
            catch (Exception exc)
            {
                Print("POSITION_SNAPSHOT_ACCOUNT_FAILED | " + exc.Message);
            }

            if (marketPosition == MarketPosition.Flat && Position != null)
            {
                marketPosition = Position.MarketPosition;
                quantity = Position.Quantity;
                averagePrice = Position.AveragePrice;
            }

            if (marketPosition == MarketPosition.Flat)
            {
                SendState("POSITION_FLAT", activeSignalId, reason);
                Print("POSITION_SNAPSHOT | state=FLAT | reason=" + reason);
                activeSignalId = string.Empty;
                entryAveragePrice = 0;
                protectedStopMoved = false;
                orderPending = false;
                return;
            }

            if (string.IsNullOrEmpty(activeSignalId))
                activeSignalId = "RECOVERED-" + DateTime.UtcNow.ToString("yyyyMMddHHmmss", CultureInfo.InvariantCulture);

            entryAveragePrice = averagePrice;
            protectedStopMoved = false;
            orderPending = false;

            string snapshotReason = reason
                + "|side=" + marketPosition
                + "|qty=" + quantity.ToString(CultureInfo.InvariantCulture)
                + "|avg=" + averagePrice.ToString(CultureInfo.InvariantCulture);
            SendState("POSITION_OPEN", activeSignalId, snapshotReason);
            Print("POSITION_SNAPSHOT | state=" + marketPosition + " | signal_id=" + activeSignalId + " | qty=" + quantity.ToString(CultureInfo.InvariantCulture) + " | avg=" + averagePrice.ToString(CultureInfo.InvariantCulture));
        }

        private string SignalEndpoint
        {
            get { return BuildEndpoint(SignalPort); }
        }

        private string StateEndpoint
        {
            get { return BuildEndpoint(StatePort); }
        }

        private static string BuildEndpoint(int port)
        {
            return "tcp://127.0.0.1:" + port.ToString(CultureInfo.InvariantCulture);
        }

        private void SignalLoop()
        {
            while (keepRunning)
            {
                try
                {
                    SendHeartbeatIfNeeded();
                    CheckPythonConnection();

                    string raw;
                    if (!signalSub.TryReceiveFrameString(TimeSpan.FromMilliseconds(100), out raw))
                        continue;

                    string type = ExtractJsonString(raw, "type").ToUpperInvariant();
                    if (type == "HEARTBEAT")
                    {
                        MarkPythonSeen();
                        continue;
                    }

                    if (type == "SIGNAL")
                    {
                        MarkPythonSeen();
                        HandleSignal(raw);
                    }
                }
                catch (Exception exc)
                {
                    Print("ZMQ_LOOP_ERROR | " + exc.Message);
                }
            }
        }

        private void HandleSignal(string raw)
        {
            string signalId = ExtractJsonString(raw, "signal_id");
            string side = ExtractJsonString(raw, "side").ToUpperInvariant();
            string expiresAtText = ExtractJsonString(raw, "expires_at_utc");

            Print("SIGNAL_RECEIVED | signal_id=" + signalId + " | side=" + side);

            if (State != State.Realtime)
            {
                RejectSignal(signalId, "not_realtime");
                return;
            }

            if (IsExpired(expiresAtText))
            {
                RejectSignal(signalId, "expired|expires_at=" + expiresAtText);
                return;
            }

            if (Position.MarketPosition != MarketPosition.Flat || orderPending)
            {
                RejectSignal(signalId, "position_or_order_active|position=" + Position.MarketPosition + "|order_pending=" + orderPending);
                return;
            }

            int quantity = ResolveQuantity();
            if (quantity < MinContracts)
            {
                RejectSignal(signalId, "invalid_quantity_or_insufficient_equity|quantity=" + quantity.ToString(CultureInfo.InvariantCulture));
                return;
            }

            string spreadRejectReason;
            if (!SpreadIsTradable(out spreadRejectReason))
            {
                RejectSignal(signalId, spreadRejectReason);
                return;
            }

            activeSignalId = signalId;
            orderPending = true;
            SendState("ACK", signalId, "accepted_by_ninja");

            if (side == "LONG")
            {
                EnterLong(quantity, "CE-Long");
            }
            else if (side == "SHORT")
            {
                EnterShort(quantity, "CE-Short");
            }
            else
            {
                orderPending = false;
                activeSignalId = string.Empty;
                RejectSignal(signalId, "unknown_side|side=" + side);
            }
        }

        private void RejectSignal(string signalId, string reason)
        {
            Print("SIGNAL_REJECTED | signal_id=" + signalId + " | reason=" + reason);
            SendState("REJECTED", signalId, reason);
        }

        private int ResolveQuantity()
        {
            if (!EnableAutoSizing)
                return Math.Max(MinContracts, Math.Min(MaxContracts, Contracts));

            double cash = 0;
            try
            {
                cash = Account.Get(AccountItem.CashValue, Currency.UsDollar);
            }
            catch
            {
                cash = 0;
            }

            double riskDollarsPerContract = StopLossTicks * TickSize * Instrument.MasterInstrument.PointValue;
            double requiredPerContract = EsMarginPerContract + riskDollarsPerContract * SizingRiskMultiple;
            if (requiredPerContract <= 0)
                return MinContracts;

            int quantity = (int)Math.Floor(cash / requiredPerContract);
            return Math.Max(MinContracts, Math.Min(MaxContracts, quantity));
        }

        private bool SpreadIsTradable(out string rejectReason)
        {
            double bid = GetCurrentBid();
            double ask = GetCurrentAsk();
            if (bid <= 0 || ask <= 0 || ask < bid)
            {
                rejectReason = "invalid_bid_ask|bid=" + bid.ToString(CultureInfo.InvariantCulture) + "|ask=" + ask.ToString(CultureInfo.InvariantCulture);
                return false;
            }

            double spreadTicks = (ask - bid) / TickSize;
            if (spreadTicks > MaxSpreadTicks)
            {
                rejectReason = "spread_too_wide|bid=" + bid.ToString(CultureInfo.InvariantCulture)
                    + "|ask=" + ask.ToString(CultureInfo.InvariantCulture)
                    + "|spread_ticks=" + spreadTicks.ToString("F2", CultureInfo.InvariantCulture)
                    + "|max_spread_ticks=" + MaxSpreadTicks.ToString(CultureInfo.InvariantCulture);
                return false;
            }

            rejectReason = string.Empty;
            return true;
        }

        private void MoveStopToProtectedPrice(double stopPrice)
        {
            if (protectedStopMoved)
                return;

            SetStopLoss(CalculationMode.Price, stopPrice);
            protectedStopMoved = true;
            Print("PROTECTED_STOP_MOVED | price=" + stopPrice.ToString(CultureInfo.InvariantCulture));
        }

        private void MarkPythonSeen()
        {
            bool wasReady = pythonReady;
            bool wasLost = pythonConnectionLostLogged;
            lastPythonHeartbeatUtc = DateTime.UtcNow;
            pythonReady = true;
            pythonConnectionLostLogged = false;

            if (!wasReady)
            {
                Print("PYTHON_READY | signal=" + SignalEndpoint);
                PublishPositionSnapshot("python_ready_snapshot");
            }
            else if (wasLost)
            {
                Print("PYTHON_RECONNECTED | signal=" + SignalEndpoint);
                PublishPositionSnapshot("python_reconnected_snapshot");
            }
        }

        private void SendHeartbeatIfNeeded()
        {
            if ((DateTime.UtcNow - lastNinjaHeartbeatUtc).TotalSeconds < HeartbeatIntervalSeconds)
                return;

            lastNinjaHeartbeatUtc = DateTime.UtcNow;
            SendState("HEARTBEAT", activeSignalId, "ninja_alive");
        }

        private void CheckPythonConnection()
        {
            if (!pythonReady || pythonConnectionLostLogged)
                return;

            double elapsed = (DateTime.UtcNow - lastPythonHeartbeatUtc).TotalSeconds;
            if (elapsed <= ConnectionTimeoutSeconds)
                return;

            pythonConnectionLostLogged = true;
            Print(string.Format(
                CultureInfo.InvariantCulture,
                "PYTHON_CONNECTION_LOST | last_seen_sec={0:F1} | timeout_sec={1}",
                elapsed,
                ConnectionTimeoutSeconds));
        }

        private void SendState(string type, string signalId, string reason)
        {
            if (statePush == null)
                return;

            string payload = "{"
                + "\"type\":\"" + EscapeJson(type) + "\","
                + "\"signal_id\":\"" + EscapeJson(signalId ?? string.Empty) + "\","
                + "\"reason\":\"" + EscapeJson(reason ?? string.Empty) + "\","
                + "\"chart_instrument\":\"" + EscapeJson(GetChartInstrumentName()) + "\","
                + "\"master_instrument\":\"" + EscapeJson(GetMasterInstrumentName()) + "\","
                + "\"account\":\"" + EscapeJson(GetAccountName()) + "\""
                + "}";

            try
            {
                statePush.SendFrame(payload);
                if (!string.Equals(type, "HEARTBEAT", StringComparison.OrdinalIgnoreCase))
                    Print("NINJA_STATE_SENT | type=" + type + " | signal_id=" + signalId + " | reason=" + reason);
            }
            catch (Exception exc)
            {
                Print("NINJA_STATE_SEND_FAILED | type=" + type + " | error=" + exc.Message);
            }
        }

        private string GetChartInstrumentName()
        {
            try
            {
                return Instrument == null ? string.Empty : Instrument.FullName;
            }
            catch
            {
                return string.Empty;
            }
        }

        private string GetMasterInstrumentName()
        {
            try
            {
                return Instrument == null || Instrument.MasterInstrument == null
                    ? string.Empty
                    : Instrument.MasterInstrument.Name;
            }
            catch
            {
                return string.Empty;
            }
        }

        private string GetAccountName()
        {
            try
            {
                return Account == null ? string.Empty : Account.Name;
            }
            catch
            {
                return string.Empty;
            }
        }

        private static bool IsExpired(string expiresAtText)
        {
            DateTime expiresAt;
            if (!DateTime.TryParse(
                    expiresAtText,
                    CultureInfo.InvariantCulture,
                    DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal,
                    out expiresAt))
            {
                return true;
            }

            return DateTime.UtcNow > expiresAt.ToUniversalTime();
        }

        private static string ExtractJsonString(string json, string key)
        {
            string needle = "\"" + key + "\"";
            int keyIndex = json.IndexOf(needle, StringComparison.OrdinalIgnoreCase);
            if (keyIndex < 0)
                return string.Empty;

            int colonIndex = json.IndexOf(':', keyIndex + needle.Length);
            if (colonIndex < 0)
                return string.Empty;

            int startQuote = json.IndexOf('"', colonIndex + 1);
            if (startQuote < 0)
                return string.Empty;

            int endQuote = json.IndexOf('"', startQuote + 1);
            if (endQuote < 0)
                return string.Empty;

            return json.Substring(startQuote + 1, endQuote - startQuote - 1);
        }

        private static string EscapeJson(string value)
        {
            return (value ?? string.Empty).Replace("\\", "\\\\").Replace("\"", "\\\"");
        }
    }

    public class TheBridgePropertyConverter : TypeConverter
    {
        private static readonly HashSet<string> HiddenProperties = new HashSet<string>
        {
            "OrderFillResolution",
            "OrderFillResolutionType",
            "FillLimitOrdersOnTouch",
            "Slippage",
            "EntriesPerDirection",
            "EntryHandling",
            "IsExitOnSessionCloseStrategy",
            "ExitOnSessionCloseSeconds",
            "StopTargetHandling",
            "SetOrderQuantity",
            "TimeInForce"
        };

        public override bool GetPropertiesSupported(ITypeDescriptorContext context)
        {
            return true;
        }

        public override PropertyDescriptorCollection GetProperties(
            ITypeDescriptorContext context,
            object value,
            Attribute[] attributes)
        {
            PropertyDescriptorCollection properties = TypeDescriptor.GetProperties(value, attributes, true);
            PropertyDescriptor[] visibleProperties = properties
                .Cast<PropertyDescriptor>()
                .Where(property => !HiddenProperties.Contains(property.Name))
                .ToArray();

            return new PropertyDescriptorCollection(visibleProperties);
        }
    }
}

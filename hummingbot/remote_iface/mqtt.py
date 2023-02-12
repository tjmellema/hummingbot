#!/usr/bin/env python

import asyncio
import logging
import threading
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

from hummingbot import get_logging_conf
from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:  # pragma: no cover
    from hummingbot.client.hummingbot_application import HummingbotApplication  # noqa: F401
    from hummingbot.core.event.event_listener import EventListener  # noqa: F401

from commlib.node import Node, NodeState
from commlib.transports.mqtt import ConnectionParameters as MQTTConnectionParameters

from hummingbot.core.event import events
from hummingbot.core.event.event_forwarder import SourceInfoEventForwarder
from hummingbot.core.pubsub import PubSub
from hummingbot.core.utils.async_utils import call_sync, safe_ensure_future
from hummingbot.notifier.notifier_base import NotifierBase
from hummingbot.remote_iface.messages import (
    MQTT_STATUS_CODE,
    BalanceLimitCommandMessage,
    BalancePaperCommandMessage,
    CommandShortcutMessage,
    ConfigCommandMessage,
    ExternalEventMessage,
    HistoryCommandMessage,
    ImportCommandMessage,
    InternalEventMessage,
    LogMessage,
    NotifyMessage,
    StartCommandMessage,
    StatusCommandMessage,
    StopCommandMessage,
)

mqtts_logger: HummingbotLogger = None


class CommandTopicSpecs:
    START: str = '/start'
    STOP: str = '/stop'
    CONFIG: str = '/config'
    IMPORT: str = '/import'
    STATUS: str = '/status'
    HISTORY: str = '/history'
    BALANCE_LIMIT: str = '/balance/limit'
    BALANCE_PAPER: str = '/balance/paper'
    COMMAND_SHORTCUT: str = '/command_shortcuts'


class TopicSpecs:
    PREFIX: str = '{namespace}/{instance_id}'
    COMMANDS: CommandTopicSpecs = CommandTopicSpecs()
    LOGS: str = '/log'
    INTERNAL_EVENTS: str = '/events'
    NOTIFICATIONS: str = '/notify'
    HEARTBEATS: str = '/hb'
    EXTERNAL_EVENTS: str = '/external/events/*'


class MQTTCommands:
    def __init__(self,
                 hb_app: "HummingbotApplication",
                 node: Node):
        if threading.current_thread() != threading.main_thread():  # pragma: no cover
            raise EnvironmentError(
                "MQTTCommands can only be initialized from the main thread."
            )
        self._hb_app = hb_app
        self._node = node
        self.logger = self._hb_app.logger
        self._ev_loop: asyncio.AbstractEventLoop = self._hb_app.ev_loop

        topic_prefix = TopicSpecs.PREFIX.format(
            namespace=self._node.namespace,
            instance_id=self._hb_app.instance_id
        )
        self._start_uri = f'{topic_prefix}{TopicSpecs.COMMANDS.START}'
        self._stop_uri = f'{topic_prefix}{TopicSpecs.COMMANDS.STOP}'
        self._config_uri = f'{topic_prefix}{TopicSpecs.COMMANDS.CONFIG}'
        self._import_uri = f'{topic_prefix}{TopicSpecs.COMMANDS.IMPORT}'
        self._status_uri = f'{topic_prefix}{TopicSpecs.COMMANDS.STATUS}'
        self._history_uri = f'{topic_prefix}{TopicSpecs.COMMANDS.HISTORY}'
        self._balance_limit_uri = f'{topic_prefix}{TopicSpecs.COMMANDS.BALANCE_LIMIT}'
        self._balance_paper_uri = f'{topic_prefix}{TopicSpecs.COMMANDS.BALANCE_PAPER}'
        self._shortcuts_uri = f'{topic_prefix}{TopicSpecs.COMMANDS.COMMAND_SHORTCUT}'

        self._init_commands()

    def _init_commands(self):
        self._node.create_rpc(
            rpc_name=self._start_uri,
            msg_type=StartCommandMessage,
            on_request=self._on_cmd_start
        )
        self._node.create_rpc(
            rpc_name=self._stop_uri,
            msg_type=StopCommandMessage,
            on_request=self._on_cmd_stop
        )
        self._node.create_rpc(
            rpc_name=self._config_uri,
            msg_type=ConfigCommandMessage,
            on_request=self._on_cmd_config
        )
        self._node.create_rpc(
            rpc_name=self._import_uri,
            msg_type=ImportCommandMessage,
            on_request=self._on_cmd_import
        )
        self._node.create_rpc(
            rpc_name=self._status_uri,
            msg_type=StatusCommandMessage,
            on_request=self._on_cmd_status
        )
        self._node.create_rpc(
            rpc_name=self._history_uri,
            msg_type=HistoryCommandMessage,
            on_request=self._on_cmd_history
        )
        self._node.create_rpc(
            rpc_name=self._balance_limit_uri,
            msg_type=BalanceLimitCommandMessage,
            on_request=self._on_cmd_balance_limit
        )
        self._node.create_rpc(
            rpc_name=self._balance_paper_uri,
            msg_type=BalancePaperCommandMessage,
            on_request=self._on_cmd_balance_paper
        )
        self._node.create_rpc(
            rpc_name=self._shortcuts_uri,
            msg_type=CommandShortcutMessage,
            on_request=self._on_cmd_command_shortcut
        )

    def _on_cmd_start(self, msg: StartCommandMessage.Request):
        response = StartCommandMessage.Response()
        timeout = 30
        try:
            if self._hb_app.strategy_name is None:
                raise Exception('Strategy check: Please import or create a strategy.')
            if self._hb_app.strategy is not None:
                raise Exception('The bot is already running - please run "stop" first')
            if msg.async_backend:
                self._hb_app.start(
                    log_level=msg.log_level,
                    script=msg.script,
                    is_quickstart=msg.is_quickstart
                )
            else:
                res = call_sync(
                    self._hb_app.start_check(),
                    loop=self._ev_loop,
                    timeout=timeout
                )
                response.msg = res if res is not None else ''
        except asyncio.exceptions.TimeoutError:
            response.msg = f'Hummingbot start command timed out after {timeout} seconds'
            response.status = MQTT_STATUS_CODE.ERROR
        except Exception as e:
            response.status = MQTT_STATUS_CODE.ERROR
            response.msg = str(e)
        return response

    def _on_cmd_stop(self, msg: StopCommandMessage.Request):
        response = StopCommandMessage.Response()
        timeout = 30
        try:
            if msg.async_backend:
                self._hb_app.stop(
                    skip_order_cancellation=msg.skip_order_cancellation
                )
            else:
                res = call_sync(
                    self._hb_app.stop_loop(),
                    loop=self._ev_loop,
                    timeout=timeout
                )
                response.msg = res if res is not None else ''
        except asyncio.exceptions.TimeoutError:
            response.msg = f'Hummingbot start command timed out after {timeout} seconds'
            response.status = MQTT_STATUS_CODE.ERROR
        except Exception as e:
            response.status = MQTT_STATUS_CODE.ERROR
            response.msg = str(e)
        return response

    def _on_cmd_config(self, msg: ConfigCommandMessage.Request):
        response = ConfigCommandMessage.Response()
        try:
            if len(msg.params) == 0:
                self._hb_app.config()
            else:
                invalid_params = []
                for param in msg.params:
                    if param[0] in self._hb_app.configurable_keys():
                        self._hb_app.config(param[0], param[1])
                        response.changes.append((param[0], param[1]))
                    else:
                        invalid_params.append(param[0])
                if len(invalid_params):
                    raise ValueError(f'Invalid param key(s): {invalid_params}')
        except Exception as e:
            response.status = MQTT_STATUS_CODE.ERROR
            response.msg = str(e)
        return response

    def _on_cmd_import(self, msg: ImportCommandMessage.Request):
        response = ImportCommandMessage.Response()
        timeout = 30  # seconds
        strategy_name = msg.strategy
        if strategy_name in (None, ''):
            response.status = MQTT_STATUS_CODE.ERROR
            response.msg = 'Empty strategy_name given!'
            return response
        strategy_file_name = f'{strategy_name}.yml'
        try:
            res = call_sync(
                self._hb_app.import_config_file(strategy_file_name),
                loop=self._ev_loop,
                timeout=timeout
            )
            response.msg = res if res is not None else ''
        except asyncio.exceptions.TimeoutError:
            response.msg = f'Hummingbot import command timed out after {timeout} seconds'
            response.status = MQTT_STATUS_CODE.ERROR
        except Exception as e:
            response.status = MQTT_STATUS_CODE.ERROR
            response.msg = str(e)
        return response

    def _on_cmd_status(self, msg: StatusCommandMessage.Request):
        response = StatusCommandMessage.Response()
        timeout = 30  # seconds
        if self._hb_app.strategy is None:
            response.status = MQTT_STATUS_CODE.ERROR
            response.msg = 'No strategy is currently running!'
            return response
        try:
            if msg.async_backend:
                self._hb_app.status()
            else:
                res = call_sync(
                    self._hb_app.strategy_status(),
                    loop=self._ev_loop,
                    timeout=timeout
                )
                response.msg = res if res is not None else ''
        except asyncio.exceptions.TimeoutError:
            response.msg = f'Hummingbot status command timed out after {timeout} seconds'
            response.status = MQTT_STATUS_CODE.ERROR
        except Exception as e:
            response.status = MQTT_STATUS_CODE.ERROR
            response.msg = str(e)
        return response

    def _on_cmd_history(self, msg: HistoryCommandMessage.Request):
        response = HistoryCommandMessage.Response()
        try:
            if msg.async_backend:
                self._hb_app.history(msg.days, msg.verbose, msg.precision)
            else:
                trades = self._hb_app.get_history_trades_json(msg.days)
                if trades:
                    response.trades = trades
        except Exception as e:
            response.status = MQTT_STATUS_CODE.ERROR
            response.msg = str(e)
        return response

    def _on_cmd_balance_limit(self, msg: BalanceLimitCommandMessage.Request):
        response = BalanceLimitCommandMessage.Response()
        try:
            data = self._hb_app.balance(
                'limit',
                [msg.exchange, msg.asset, msg.amount]
            )
            response.data = data
        except Exception as e:
            response.status = MQTT_STATUS_CODE.ERROR
            response.msg = str(e)
        return response

    def _on_cmd_balance_paper(self, msg: BalancePaperCommandMessage.Request):
        response = BalancePaperCommandMessage.Response()
        try:
            data = self._hb_app.balance(
                'paper',
                [msg.asset, msg.amount]
            )
            response.data = data
        except Exception as e:
            response.status = MQTT_STATUS_CODE.ERROR
            response.msg = str(e)
        return response

    def _on_cmd_command_shortcut(self, msg: CommandShortcutMessage.Request):
        response = CommandShortcutMessage.Response()
        try:
            for param in msg.params:
                response.success.append(self._hb_app._handle_shortcut(param))
        except Exception as e:
            response.status = MQTT_STATUS_CODE.ERROR
            response.msg = str(e)
        return response


class MQTTMarketEventForwarder:
    @classmethod
    def logger(cls) -> HummingbotLogger:
        global mqtts_logger
        if mqtts_logger is None:  # pragma: no cover
            mqtts_logger = HummingbotLogger(__name__)
        return mqtts_logger

    def __init__(self,
                 hb_app: "HummingbotApplication",
                 node: Node):
        if threading.current_thread() != threading.main_thread():  # pragma: no cover
            raise EnvironmentError(
                "MQTTMarketEventForwarder can only be initialized from the main thread."
            )
        self._hb_app = hb_app
        self._node = node
        self._ev_loop: asyncio.AbstractEventLoop = self._hb_app.ev_loop
        self._markets: List[ConnectorBase] = list(self._hb_app.markets.values())

        topic_prefix = TopicSpecs.PREFIX.format(
            namespace=self._node.namespace,
            instance_id=self._hb_app.instance_id
        )
        self._topic = f'{topic_prefix}{TopicSpecs.INTERNAL_EVENTS}'

        self._mqtt_fowarder: SourceInfoEventForwarder = \
            SourceInfoEventForwarder(self._send_mqtt_event)
        self._market_event_pairs: List[Tuple[int, EventListener]] = [
            (events.MarketEvent.BuyOrderCreated, self._mqtt_fowarder),
            (events.MarketEvent.BuyOrderCompleted, self._mqtt_fowarder),
            (events.MarketEvent.SellOrderCreated, self._mqtt_fowarder),
            (events.MarketEvent.SellOrderCompleted, self._mqtt_fowarder),
            (events.MarketEvent.OrderFilled, self._mqtt_fowarder),
            (events.MarketEvent.OrderFailure, self._mqtt_fowarder),
            (events.MarketEvent.OrderCancelled, self._mqtt_fowarder),
            (events.MarketEvent.OrderExpired, self._mqtt_fowarder),
            (events.MarketEvent.FundingPaymentCompleted, self._mqtt_fowarder),
            (events.MarketEvent.RangePositionLiquidityAdded, self._mqtt_fowarder),
            (events.MarketEvent.RangePositionLiquidityRemoved, self._mqtt_fowarder),
            (events.MarketEvent.RangePositionUpdate, self._mqtt_fowarder),
            (events.MarketEvent.RangePositionUpdateFailure, self._mqtt_fowarder),
            (events.MarketEvent.RangePositionFeeCollected, self._mqtt_fowarder),
            (events.MarketEvent.RangePositionClosed, self._mqtt_fowarder),
        ]

        self.event_fw_pub = self._node.create_publisher(
            topic=self._topic, msg_type=InternalEventMessage
        )
        self._start_event_listeners()

    def _send_mqtt_event(self, event_tag: int, pubsub: PubSub, event):
        if threading.current_thread() != threading.main_thread():  # pragma: no cover
            self._ev_loop.call_soon_threadsafe(
                self._send_mqtt_event,
                event_tag,
                pubsub,
                event
            )
            return
        try:
            event_types = {
                events.MarketEvent.BuyOrderCreated.value: "BuyOrderCreated",
                events.MarketEvent.BuyOrderCompleted.value: "BuyOrderCompleted",
                events.MarketEvent.SellOrderCreated.value: "SellOrderCreated",
                events.MarketEvent.SellOrderCompleted.value: "SellOrderCompleted",
                events.MarketEvent.OrderFilled.value: "OrderFilled",
                events.MarketEvent.OrderCancelled.value: "OrderCancelled",
                events.MarketEvent.OrderExpired.value: "OrderExpired",
                events.MarketEvent.OrderFailure.value: "OrderFailure",
                events.MarketEvent.FundingPaymentCompleted.value: "FundingPaymentCompleted",
                events.MarketEvent.RangePositionLiquidityAdded.value: "RangePositionLiquidityAdded",
                events.MarketEvent.RangePositionLiquidityRemoved.value: "RangePositionLiquidityRemoved",
                events.MarketEvent.RangePositionUpdate.value: "RangePositionUpdate",
                events.MarketEvent.RangePositionUpdateFailure.value: "RangePositionUpdateFailure",
                events.MarketEvent.RangePositionFeeCollected.value: "RangePositionFeeCollected",
                events.MarketEvent.RangePositionClosed.value: "RangePositionClosed",
            }
            event_type = event_types[event_tag]
        except KeyError:
            event_type = "Unknown"

        if is_dataclass(event):
            event_data = asdict(event)
        elif isinstance(event, tuple) and hasattr(event, '_fields'):
            event_data = event._asdict()
        else:
            try:
                event_data = dict(event)
            except (TypeError, ValueError):
                event_data = {}

        try:
            timestamp = event_data.pop('timestamp')
        except KeyError:
            timestamp = datetime.now().timestamp()

        if 'type' in event_data:
            if not isinstance(event_data.get('type', None), str):
                event_data['type'] = str(event_data['type'])

        self.event_fw_pub.publish(
            InternalEventMessage(
                timestamp=int(timestamp),
                type=event_type,
                data=event_data
            )
        )

    def _start_event_listeners(self):
        for market in self._markets:
            for event_pair in self._market_event_pairs:
                market.add_listener(event_pair[0], event_pair[1])
                self.logger().debug(
                    f'Created MQTT bridge for event: {event_pair[0]}, {event_pair[1]}'
                )

    def _stop_event_listeners(self):
        for market in self._markets:
            for event_pair in self._market_event_pairs:
                market.remove_listener(event_pair[0], event_pair[1])


class MQTTNotifier(NotifierBase):
    def __init__(self,
                 hb_app: "HummingbotApplication",
                 node: Node) -> None:
        super().__init__()
        self._node = node
        self._hb_app = hb_app
        self._ev_loop: asyncio.AbstractEventLoop = self._hb_app.ev_loop

        topic_prefix = TopicSpecs.PREFIX.format(
            namespace=self._node.namespace,
            instance_id=self._hb_app.instance_id
        )
        self._topic = f'{topic_prefix}{TopicSpecs.NOTIFICATIONS}'
        self.notify_pub = self._node.create_publisher(
            topic=self._topic,
            msg_type=NotifyMessage
        )

    def add_msg_to_queue(self, msg: str):
        if threading.current_thread() != threading.main_thread():  # pragma: no cover
            self._ev_loop.call_soon_threadsafe(self.add_msg_to_queue, msg)
            return
        self.notify_pub.publish(NotifyMessage(msg=msg))

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class MQTTGateway(Node):
    NODE_NAME: str = 'hbot.$instance_id'
    _instance: Optional["MQTTGateway"] = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        global mqtts_logger
        if mqtts_logger is None:  # pragma: no cover
            mqtts_logger = logging.getLogger(__name__)
        return mqtts_logger

    @classmethod
    def main(cls) -> "MQTTGateway":
        return cls._instance

    def __init__(self,
                 hb_app: "HummingbotApplication",
                 *args, **kwargs
                 ):
        self._health = False
        self._stop_event_async = asyncio.Event()
        self._notifier: MQTTNotifier = None
        self._market_events: MQTTMarketEventForwarder = None
        self._commands: MQTTCommands = None
        self._logh: MQTTLogHandler = None
        self._external_events: MQTTExternalEvents = None
        self._hb_app: "HummingbotApplication" = hb_app
        self._ev_loop = self._hb_app.ev_loop
        self._params = self._create_mqtt_params_from_conf()
        self.namespace = self._hb_app.client_config_map.mqtt_bridge.mqtt_namespace
        if self.namespace[-1] in ('/', '.'):
            self.namespace = self.namespace[:-1]

        self._topic_prefix = TopicSpecs.PREFIX.format(
            namespace=self.namespace,
            instance_id=self._hb_app.instance_id
        )
        _hb_topic = f'{self._topic_prefix}{TopicSpecs.HEARTBEATS}'

        super().__init__(
            node_name=self.NODE_NAME.replace('$instance_id', hb_app.instance_id),
            connection_params=self._params,
            heartbeats=True,
            heartbeat_uri=_hb_topic,
            *args,
            **kwargs
        )
        MQTTGateway._instance = self

    @property
    def health(self):
        return self._health

    def _remove_log_handlers(self):
        loggers = [logging.getLogger(name) for name in logging.root.manager.loggerDict]
        log_conf = get_logging_conf()
        if 'loggers' not in log_conf:
            return
        logs = [key for key, val in log_conf.get('loggers').items()]
        for logger in loggers:
            if 'hummingbot' in logger.name:
                for log in logs:
                    if log in logger.name:
                        self.remove_log_handler(logger)
        self._logh = None

    def _init_logger(self):
        self._logh = MQTTLogHandler(self._hb_app, self)
        self.patch_loggers()

    def patch_loggers(self):
        loggers = [logging.getLogger(name) for name in logging.root.manager.loggerDict]

        log_conf = get_logging_conf()
        if 'root' in log_conf:
            if log_conf.get('root').get('mqtt'):
                self.remove_log_handler(self._get_root_logger())
                self.add_log_handler(self._get_root_logger())

        if 'loggers' not in log_conf:
            return
        log_conf_names = [key for key, val in log_conf.get('loggers').items()]
        loggers_filtered = [logger for logger in loggers if
                            logger.name in log_conf_names]
        loggers_filtered = [logger for logger in loggers_filtered if
                            log_conf.get('loggers').get(logger.name).get('mqtt', False)]

        for logger in loggers_filtered:
            self.remove_log_handler(logger)
            self.add_log_handler(logger)

    def _get_root_logger(self):
        return logging.getLogger()

    def remove_log_handler(self, logger: HummingbotLogger):
        logger.removeHandler(self._logh)

    def add_log_handler(self, logger: HummingbotLogger):
        logger.addHandler(self._logh)

    def _init_notifier(self):
        if self._hb_app.client_config_map.mqtt_bridge.mqtt_notifier:
            self._notifier = MQTTNotifier(self._hb_app, self)
            self._hb_app.notifiers.append(self._notifier)

    def _remove_notifier(self):
        self._hb_app.notifiers.remove(self._notifier) if self._notifier \
            in self._hb_app.notifiers else None

    def _init_commands(self):
        if self._hb_app.client_config_map.mqtt_bridge.mqtt_commands:
            self._commands = MQTTCommands(self._hb_app, self)

    def start_market_events_fw(self):
        # Must be called after loading the strategy.
        # HummingbotApplication._initialize_markets() must be be called before
        if self._hb_app.client_config_map.mqtt_bridge.mqtt_events:
            self._market_events = MQTTMarketEventForwarder(self._hb_app, self)
            if self.state == NodeState.RUNNING:
                self._market_events.event_fw_pub.run()

    def _remove_market_event_listeners(self):
        if self._market_events is not None:
            self._market_events._stop_event_listeners()

    def _init_external_events(self):
        if self._hb_app.client_config_map.mqtt_bridge.mqtt_external_events:
            self._external_events = MQTTExternalEvents(self._hb_app, self)

    def add_external_event_listener(self,
                                    callback: Callable[[ExternalEventMessage, str], None]):
        self._external_events.add_global_listener(callback)

    def remove_external_event_listener(self,
                                       callback: Callable[[ExternalEventMessage, str], None]):
        self._external_events.remove_global_listener(callback)

    def _create_mqtt_params_from_conf(self):
        host = self._hb_app.client_config_map.mqtt_bridge.mqtt_host
        port = self._hb_app.client_config_map.mqtt_bridge.mqtt_port
        username = self._hb_app.client_config_map.mqtt_bridge.mqtt_username
        password = self._hb_app.client_config_map.mqtt_bridge.mqtt_password
        ssl = self._hb_app.client_config_map.mqtt_bridge.mqtt_ssl
        conn_params = MQTTConnectionParameters(
            host=host,
            port=int(port),
            username=username,
            password=password,
            ssl=ssl
        )
        return conn_params

    def _check_connections(self) -> bool:
        for c in self._publishers:
            if not c._transport.is_connected:
                return False
        for c in self._rpc_services:
            if not c._transport.is_connected:
                return False
        # Will use if subscribtions are integrated
        for c in self._subscribers:
            if not c._transport.is_connected:
                return False
        # Will use if rpc clients are integrated
        # for c in self._rpc_clients:
        #     if not c._transport.is_connected:
        #         return False
        return True

    def _start_health_monitoring_loop(self):
        if threading.current_thread() != threading.main_thread():  # pragma: no cover
            self._ev_loop.call_soon_threadsafe(self.start_check_health_loop)
            return
        self._stop_event_async.clear()
        safe_ensure_future(self._monitor_health_loop(),
                           loop=self._ev_loop)

    async def _monitor_health_loop(self, period: float = 1.0):
        while not self._stop_event_async.is_set():
            # Maybe we can include more checks here to determine the health!
            self._health = await self._ev_loop.run_in_executor(
                None, self._check_connections)
            await asyncio.sleep(period)

    def _stop_health_monitorint_loop(self):
        self._stop_event_async.set()

    def start(self) -> None:
        self._init_logger()
        self._init_notifier()
        self._init_commands()
        self._init_external_events()
        self._start_health_monitoring_loop()
        self.run()

    def stop(self):
        super().stop()
        self._remove_notifier()
        self._remove_log_handlers()
        self._remove_market_event_listeners()
        self._stop_health_monitorint_loop()

    def __del__(self):
        self.stop()


class MQTTLogHandler(logging.Handler):
    def __init__(self,
                 hb_app: "HummingbotApplication",
                 node: Node):
        if threading.current_thread() != threading.main_thread():  # pragma: no cover
            raise EnvironmentError(
                "MQTTLogHandler can only be initialized from the main thread."
            )
        self._hb_app = hb_app
        self._node = node
        self._ev_loop: asyncio.AbstractEventLoop = self._hb_app.ev_loop

        topic_prefix = TopicSpecs.PREFIX.format(
            namespace=self._node.namespace,
            instance_id=self._hb_app.instance_id
        )
        self._topic = f'{topic_prefix}{TopicSpecs.LOGS}'

        super().__init__()
        self.name = self.__class__.__name__
        self.log_pub = self._node.create_publisher(topic=self._topic,
                                                   msg_type=LogMessage)

    def emit(self, record: logging.LogRecord):
        if threading.current_thread() != threading.main_thread():  # pragma: no cover
            self._ev_loop.call_soon_threadsafe(self.emit, record)
            return
        msg_str = self.format(record)
        msg = LogMessage(
            timestamp=time.time(),
            msg=msg_str,
            level_no=record.levelno,
            level_name=record.levelname,
            logger_name=record.name

        )
        self.log_pub.publish(msg)


class MQTTExternalEvents:
    def __init__(self,
                 hb_app: "HummingbotApplication",
                 node: Node
                 ):
        self._node: Node = node
        self._hb_app: 'HummingbotApplication' = hb_app
        self._ev_loop: asyncio.AbstractEventLoop = self._hb_app.ev_loop

        topic_prefix = TopicSpecs.PREFIX.format(
            namespace=self._node.namespace,
            instance_id=self._hb_app.instance_id
        )
        self._topic = f'{topic_prefix}{TopicSpecs.EXTERNAL_EVENTS}'

        self._node.create_psubscriber(
            topic=self._topic,
            msg_type=ExternalEventMessage,
            on_message=self._on_event_arrived
        )
        self._listeners: Dict[
            str,
            List[Callable[ExternalEventMessage, str], None]
        ] = {'*': []}

    def _event_uri_to_name(self, topic: str) -> str:
        return topic.split('events/')[1].replace('/', '.')

    def _on_event_arrived(self,
                          msg: ExternalEventMessage,
                          topic: str
                          ) -> None:
        if threading.current_thread() != threading.main_thread():  # pragma: no cover
            self._ev_loop.call_soon_threadsafe(self._on_event_arrived,
                                               msg, topic)
            return
        event_name = self._event_uri_to_name(topic)
        self._hb_app.logger().debug(
            f'Received external event {event_name} -> {msg} - '
            'Broadcasting to listeners...'
        )
        if event_name in self._listeners:
            for fenc in self._listeners[event_name]:
                fenc(msg, event_name)
        for fenc in self._listeners['*']:
            fenc(msg, event_name)

    def add_listener(self,
                     event_name: str,
                     callback: Callable[[ExternalEventMessage, str], None]
                     ) -> None:
        # TODO validate event_name with regex
        if event_name in self._listeners:
            self._listeners.get(event_name).append(callback)

    def remove_listener(self,
                        event_name: str,
                        callback: Callable[[ExternalEventMessage, str], None]
                        ):
        # TODO validate event_name with regex
        if event_name in self._listeners:
            self._listeners.get(event_name).remove(callback)

    def add_global_listener(self,
                            callback: Callable[[ExternalEventMessage, str], None]
                            ):
        if '*' in self._listeners:
            self._listeners.get('*').append(callback)

    def remove_global_listener(self,
                               callback: Callable[[ExternalEventMessage, str], None]
                               ):
        if '*' in self._listeners:
            self._listeners.get('*').remove(callback)


class MQTTTopicListener:
    def __init__(self,
                 topic: str,
                 on_message: Callable[[Dict, str], None],
                 use_topic_prefix: Optional[bool] = True
                 ):
        self._node = MQTTGateway.main()
        if self._node is None:
            raise Exception('MQTT Gateway not yet initialized')
        topic_prefix = TopicSpecs.PREFIX.format(
            namespace=self._node.namespace,
            instance_id=self._node._hb_app.instance_id
        )
        if use_topic_prefix:
            self._topic = f'{topic_prefix}{topic}'
        else:
            self._topic = topic
        self._on_message = on_message
        s = self._node.create_psubscriber(topic=self._topic,
                                          on_message=self._on_message)
        if self._node.state == NodeState.RUNNING:
            s.run()

import asyncio
from datetime import datetime, timedelta, timezone
from discord.ext import tasks
from pypika import Query, Table, Column
from typing import Union, Dict, Tuple
import re

from lib.rcon import HLLRcon
from lib.credentials import Credentials
from lib.storage import LogLine, database, cursor, insert_many_logs, delete_logs
from lib.exceptions import NotFound, SessionDeletedError, SessionAlreadyRunningError, SessionMissingCredentialsError
from lib.modifiers import ModifierFlags
from lib.info.models import EventFlags, EventModel, ActivationEvent, IterationEvent, DeactivationEvent, InfoHopper
from lib.info.events import EventListener
from utils import get_config, schedule_coro, get_logger

SECONDS_BETWEEN_ITERATIONS = get_config().getint('Session', 'SecondsBetweenIterations')
NUM_LOGS_REQUIRED_FOR_INSERT = get_config().getint('Session', 'NumLogsRequiredForInsert')
DELETE_SESSION_AFTER = timedelta(days=get_config().getint('Session', 'DeleteAfterDays'))
SESSIONS: Dict[int, 'HLLCaptureSession'] = dict()

def assert_name(name: str):
    return re.sub(r"[^\w\(\)_\-,\.]", "_", name)

def get_sessions(guild_id: int):
    return sorted([sess for sess in SESSIONS.values() if sess.guild_id == guild_id], key=lambda sess: sess.start_time)

class HLLCaptureSession:
    def __init__(self, id: int, guild_id: int, name: str, start_time: datetime, end_time: datetime,
            credentials: Credentials, modifiers: ModifierFlags = ModifierFlags(), loop: asyncio.AbstractEventLoop = None):
        self.id = id
        self.guild_id = guild_id
        self.name = assert_name(name)
        self.start_time = start_time
        self.end_time = end_time
        self.credentials = credentials
        self.loop = loop or asyncio.get_running_loop()
        self._logs = list()

        if self.id in SESSIONS:
            raise SessionAlreadyRunningError("A session with ID %s is already running")

        self.logger = get_logger(self)

        self.rcon = None
        self.info = None

        self.modifiers = [modifier(self) for modifier in modifiers.get_modifier_types()]
        self.modifier_flags = modifiers.copy()
        if self.modifiers:
            self.logger.info("Installed modifiers: %s", ", ".join([modifier.config.name for modifier in self.modifiers]))
        
        if self.active_in():
            self._start_task = schedule_coro(self.start_time, self.activate, error_logger=self.logger)
            self._stop_task = schedule_coro(self.end_time, self.deactivate, error_logger=self.logger)
        else:
            self._start_task = None
            self._stop_task = None

        self.__listeners = None

        SESSIONS[self.id] = self
        
    @classmethod
    def load_from_db(cls, id: int):
        cursor.execute('SELECT ROWID, guild_id, name, start_time, end_time, deleted, credentials_id, modifiers FROM sessions WHERE ROWID = ?', (id,))
        res = cursor.fetchone()

        if not res:
            raise NotFound(f"No session exists with ID {id}")

        deleted = bool(res[5])
        if deleted:
            raise SessionDeletedError

        credentials_id = res[6]
        if credentials_id is None:
            credentials = None
        else:
            credentials = Credentials.load_from_db(credentials_id)

        return cls(
            id=int(res[0]),
            guild_id=int(res[1]),
            name=str(res[2]),
            start_time=datetime.fromisoformat(res[3]),
            end_time=datetime.fromisoformat(res[4]),
            credentials=credentials,
            modifiers=ModifierFlags(int(res[7]))
        )
    
    @classmethod
    def create_in_db(cls, guild_id: int, name: str, start_time: datetime, end_time: datetime, credentials: Credentials, modifiers: ModifierFlags = ModifierFlags()):
        if datetime.now(tz=timezone.utc) > end_time:
            raise ValueError('This capture session would have already ended')

        name = assert_name(name)

        cursor.execute('INSERT INTO sessions (guild_id, name, start_time, end_time, credentials_id, modifiers) VALUES (?,?,?,?,?,?)',
            (guild_id, name, start_time, end_time, credentials.id, modifiers.value))
        id_ = cursor.lastrowid

        # Create the table if needed
        sess_name = f"session{id_}"
        table = Table(sess_name)
        create_table_query = Query.create_table(table).columns(*[
            Column(field.name, 'TEXT') for field in LogLine.__fields__.values()
        ])
        cursor.execute(str(create_table_query))
        database.commit()

        return cls(
            id=id_,
            guild_id=guild_id,
            name=name,
            start_time=start_time,
            end_time=end_time,
            credentials=credentials,
            modifiers=modifiers
        )

    @property
    def duration(self):
        return self.end_time - self.start_time

    def __str__(self):
        return f"{self.name} ({self.credentials.name})" if self.credentials else f"{self.name} (⚠️)"

    def save(self):
        cursor.execute("""UPDATE sessions SET name = ?, start_time = ?, end_time = ?, credentials_id = ?, modifiers = ? WHERE ROWID = ?""",
            (self.name, self.start_time, self.end_time, self.credentials.id if self.credentials else None, self.modifier_flags.value, self.id))
        database.commit()

    def active_in(self) -> Union[timedelta, bool]:
        """Returns how long until the session should start. Otherwise
        returns whether the session should currently be active or not.

        Returns
        -------
        Union[timedelta, bool]
            The time until the session should activate, otherwise
            whether it is currently active.
        """
        now = datetime.now(tz=timezone.utc)
        if self.start_time > now:
            return now - self.start_time
        else:
            return self.end_time > now
    
    def should_delete(self):
        return datetime.now(tz=timezone.utc) > (self.end_time + DELETE_SESSION_AFTER)

    async def activate(self):
        if not self.credentials:
            raise SessionMissingCredentialsError(f"Session with ID {self.id} does not have server credentials")
        if self.rcon is None:
            self.rcon = HLLRcon(session=self)
        self.gatherer.start()
    async def deactivate(self):
        self.gatherer.stop()
        # self.push_to_db()   This is handled by the gather after_loop

    async def stop(self):
        active = self.active_in()

        if active == False:
            pass
        elif active == True:
            self.end_time = datetime.now(tz=timezone.utc)
            self.save()
        else:
            self.start_time = self.end_time = datetime.now(tz=timezone.utc)
            self.save()
        
        await self.deactivate()
        self._clear_tasks()

    @tasks.loop(seconds=SECONDS_BETWEEN_ITERATIONS)
    async def gatherer(self):
        info = await self.rcon.update()

        if not info:
            return

        if self.info:
            info.compare_older(self.info, event_time=self.rcon._logs_seen_time)
        self.info = info
        
        events = list(info.events.flatten())
        events.insert(0, IterationEvent(info))
        for event in events:
            try:
                log = LogLine.from_event(event)
                # print(event.to_dict(exclude_unset=True))
            except:
                self.logger.exception('Failed to cast event to log line: %s %s' % (type(event).__name__, event.to_dict(exclude_unset=True)))
            else:
                self._logs.append(log)
            
            for modifier in self.modifiers:
                for listener in modifier.get_listeners_for_event(event):
                    asyncio.create_task(listener.invoke(modifier, event))
            
        if len(self._logs) > NUM_LOGS_REQUIRED_FOR_INSERT:
            self.push_to_db()

    @gatherer.before_loop
    async def before_gatherer_start(self):
        try:
            await self.rcon.start(force=False)
        except Exception:
            self.logger.exception('Failed to start RCON')
        
        event = ActivationEvent(InfoHopper())
        coros = list()
        for modifier in self.modifiers:
            for listener in modifier.get_listeners_for_event(event):
                coros.append(listener.invoke(modifier, event))
        if coros:
            await asyncio.gather(*coros)

    @gatherer.after_loop
    async def after_gatherer_stop(self):
        event = DeactivationEvent(self.info)
        coros = list()
        for modifier in self.modifiers:
            for listener in modifier.get_listeners_for_event(event):
                coros.append(listener.invoke(modifier, event))
        if coros:
            await asyncio.gather(*coros)

        try:
            await self.rcon.stop(force=True)
        except Exception:
            self.logger.exception('Failed to stop RCON')
        self.push_to_db()

    def _clear_tasks(self):
        if self._start_task and not self._start_task.done():
            self._start_task.cancel()
        if self._stop_task and not self._stop_task.done():
            self._stop_task.cancel()

    @property
    def listeners(self) -> Dict[str, Tuple[EventListener]]:
        if self.__listeners is None:
            listeners = dict()
            for modifier in self.modifiers:
                for event_type, values in modifier.listeners.items():
                    listeners.setdefault(event_type, list()).extend(values)
            self.__listeners = {event_type: tuple(values) for event_type, values in listeners.items()}
        return self.__listeners
    def get_listeners_for_event(self, event: EventModel):
        """Returns a generator of all listeners that listen for this event

        Parameters
        ----------
        event : EventModel
            The event

        Yields
        ------
        EventListener
            A corresponding event listener
        """
        yield from self.listeners.get(event.event_time, list())

    def push_to_db(self):
        self.logger.info('Pushing %s logs to the DB', len(self._logs))
        if self._logs:
            insert_many_logs(sess_id=self.id, logs=self._logs)
        self._logs = list()

    def get_logs(self, from_: datetime = None, to: datetime = None, filter: EventFlags = None, limit: int = None):
        self.push_to_db()

        sess_name = f"session{self.id}"
        columns = tuple(LogLine.__fields__)

        table = Table(sess_name)
        query = table.select(*columns)

        if from_:
            query = query.where(table.event_time >= from_)
        if to:
            query = query.where(table.event_time < to)
        if filter is not None:
            query = query.where(table.type.isin([k for k, v in filter if v]))
        if limit:
            query = query.limit(limit)
        
        cursor.execute(str(query))
        return [LogLine(
            **{k: v for k, v in zip(columns, record) if v is not None}
        ) for record in cursor.fetchall()]

    def delete(self):
        self.logger.info('Deleting session...')
        schedule_coro(datetime.now(tz=timezone.utc), self.deactivate, error_logger=self.logger)
        self._clear_tasks()
        delete_logs(sess_id=self.id)

        table = Table("sessions")
        update_query = table.update().set(table.deleted, True).where(table.ROWID == self.id)
        cursor.execute(str(update_query))
        database.commit()
        
        del SESSIONS[self.id]

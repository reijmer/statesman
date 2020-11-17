"""Statesman is a modern state machine library."""
from __future__ import annotations

import asyncio
import collections
import contextlib
import datetime
import enum
import functools
import inspect
import types
import typing
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional, Sequence, Set, Tuple, Type, Union

import pydantic

__all__ = [
    'StateEnum',
    'State',
    'Transition',
    'Event',
    'InitialState',
    'StateMachine',
    'HistoryMixin',
    'event',
    'enter_state',
    'exit_state',
    'guard_event',
    'before_event',
    'on_event',
    'after_event',
]


class classproperty:
    """Decorator that transforms a method with a single cls argument into a
    property that can be accessed directly from the class."""

    def __init__(self, method=None):
        self.fget = method

    def __get__(self, instance, cls=None):
        return self.fget(cls)

    def getter(self, method):
        self.fget = method
        return self


ActiveState = Literal['__active__']


class InitialState(str):
    """Declares the initial state in a state machine."""


class StateEnum(enum.Enum):
    """An abstract enumeration base class for defining states within a state
    machine.

    State enumerations are interpreted as describing states where the
    `name` attribute defines the unique, symbolic name of the state
    within the state machine while the `value` attribute defines the
    human readable description.
    """
    @classproperty
    def __any__(cls) -> 'List[StateEnum]':
        """Return a list of all members of the enumeration for use when any
        state is available."""
        return list(cls)

    @classproperty
    def __active__(cls) -> str:
        """Return a sentinel string value that indicates that the state is to
        remain the currently active value."""
        return '__active__'

    @classproperty
    def __initial__(cls) -> Optional['StateEnum']:
        """Return the initial state member as annotated via the
        statesman.InitialState class."""
        return next(filter(lambda s: isinstance(s.value, InitialState), list(cls)), None)

    def __init__(self, description: str) -> None:
        if self.__initial__ and isinstance(description, InitialState):
            raise ValueError(f"cannot declare more than one initial state: \"{self.__initial__}\" already declared")


class Action(pydantic.BaseModel):
    """An Action is a callable object attached to states and events within a
    state machine."""
    class Types(str, enum.Enum):
        """An enumeration that defines the types of actions that can be
        attached to states and events."""

        # State actions
        entry = 'entry'
        exit = 'exit'

        # Event actions
        guard = 'guard'
        before = 'before'
        on = 'on'
        after = 'after'

    callable: Callable
    signature: inspect.Signature
    type: Optional[Action.Types] = None

    @pydantic.root_validator(pre=True)
    @classmethod
    def _cache_signature(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        if callable := values.get('callable', None):
            values['signature'] = inspect.Signature.from_callable(callable)

        return values

    async def __call__(self, *args, **kwargs) -> Any:
        """Call the action with the matching parameters and return the
        result."""
        matched_args, matched_kwargs = _parameters_matching_signature(self.signature, *args, **kwargs)
        if asyncio.iscoroutinefunction(self.callable):
            return await self.callable(*matched_args, **matched_kwargs)
        else:
            return self.callable(*matched_args, **matched_kwargs)

    class Config:
        arbitrary_types_allowed = True


Action.update_forward_refs()


class BaseModel(pydantic.BaseModel):
    """Provides common functionality for statesman models."""
    _actions: List[Action] = pydantic.PrivateAttr([])

    def _add_action(self, action: Action) -> None:
        """Add a action."""
        self._actions.append(action)

    def _remove_action(self, action: Action) -> None:
        """Remove a action."""
        self._actions.remove(action)

    def _remove_actions(self, actions: Union[None, List[Action], Action.Types] = None) -> None:
        """Remove a collection of actions."""
        if actions is None:
            self._actions = []
        elif isinstance(actions, list):
            for action in self._actions:
                if action in actions:
                    self._actions.remove(action)
        elif isinstance(actions, Action.Types):
            for action in self._actions:
                if action.type == actions:
                    self._actions.remove(action)
        else:
            raise ValueError(f'invalid argument: {actions}')

    def _get_actions(self, type_: Action.Types) -> List[Action]:
        """Retrieve a subset of actions by type."""
        return list(filter(lambda c: c.type == type_, self._actions))

    async def _run_actions(self, type_: Action.Types, *args, **kwargs) -> List[Any]:
        return await asyncio.gather(*(action(*args, **kwargs) for action in self._get_actions(type_)))


class State(BaseModel):
    """Models a state within a state machine.

    State objects can be tested for equality against `str` and `StateEnum` objects.

    Attributes:
        name: A unique name of the state within the state machine.
        description: An optional description of the state.
    """
    name: str
    description: Optional[str] = None

    @classmethod
    def from_enum(cls, class_: Type[StateEnum]) -> List['State']:
        """Return a list of State objects from a state enum subclass."""
        states = []
        if inspect.isclass(class_) and issubclass(class_, StateEnum):
            for item in class_:
                states.append(cls(name=item.name, description=item.value))
        else:
            raise TypeError(f"invalid parameter: \"{class_.__class__.__name__}\" is not a StateEnum subclass: {class_}")

        return states

    @classmethod
    def active(cls) -> 'State':
        """Return a temporary state object that represents the active state at
        a future point in time."""
        return State(name='__active__', description='The active state at transition time.')

    @pydantic.validator('name', 'description', pre=True)
    @classmethod
    def _value_from_base_states(cls, value: Union[str, StateEnum], field) -> str:
        """Extract the appropriate value for the model field from a States
        enumeration value.

        States objects are serialized differently than typical Enum
        values in Pydantic. The name field is used to populate the state
        name and the value populates the description.
        """
        if isinstance(value, StateEnum):
            if field.name == 'name':
                return value.name
            elif field.name == 'description':
                return value.value

        return value

    def __init__(self, name: str, description: Optional[str] = None) -> None:
        super().__init__(name=name, description=description)

    def __eq__(self, other) -> bool:
        if isinstance(other, StateEnum):
            return self.name == other.name
        elif isinstance(other, str):
            return self.name == other
        else:
            return super().__eq__(other)

    def __hash__(self):
        return hash(self.name)

    @property
    def actions(self) -> List[Action]:
        """Return a list of entry and exit actions attached to the state."""
        return self._actions.copy()

    def get_actions(self, type_: Literal[Action.Types.entry, Action.Types.exit]) -> List[Action]:
        """Return a list of all entry or exit actions attached to the state."""
        return super()._get_actions(type_)

    def add_action(self, callable: Callable, type_: Literal[Action.Types.entry, Action.Types.exit]) -> Action:
        """Add an entry or exit action to the state."""
        acceptable_types = (Action.Types.entry, Action.Types.exit)
        if type_ not in acceptable_types:
            raise ValueError(
                f"cannot add state action with type \"{type_}\": must be {_summarize(acceptable_types, conjunction='or', quote=True)}", )
        action = Action(callable=callable, type=type_)
        super()._add_action(action)
        return action

    def remove_action(self, action: Action) -> Action:
        """Remove a action from the state."""
        return super()._remove_action(action)

    def remove_actions(
        self, actions: Union[None, List[Action], Literal[Action.Types.entry, Action.Types.exit]] = None,
    ) -> None:
        """Remove actions that are attached to the state.

        There are three modes of operation:
        - Passing a value of `None` (the default) will remove all actions.
        - Passing a specific set of `Action` objects will remove only those actions.
        - Passing `Action.Types.enter` or `Action.Types.exit` will remove all actions that match the given type.
        """
        return super()._remove_actions(actions)


class Event(BaseModel):
    """Event objects model something that happens within a state machine that
    triggers a state transition.

    Attributes:
        name: A unique name of the event within the state machine.
        description: An optional description of the event.
        sources: A list of states that the event can be triggered from. The inclusion of `None` denotes an initialization event.
        target: The state that the state machine will transition into at the completion of the event.
        transition_type: An optional type specifying how the transition triggered by the event should be performed.
    """
    name: str
    description: Optional[str] = None
    sources: List[Union[None, State]]
    target: State
    transition_type: Optional['Transition.Types']

    @property
    def actions(self) -> List[Action]:
        """Return a list of actions attached to the event."""
        return self._actions.copy()

    def get_actions(
        self, type_: Literal[Action.Types.guard, Action.Types.before, Action.Types.after],
    ) -> List[Action]:
        """Return a list of all guard, before, or after actions attached to the
        event."""
        return super()._get_actions(type_)

    def add_action(
        self,
        callable: Callable,
        type_: Literal[Action.Types.guard, Action.Types.before, Action.Types.on, Action.Types.after],
    ) -> Action:
        """Add a guard, before, on, or after action to the event."""
        acceptable_types = (Action.Types.guard, Action.Types.before, Action.Types.on, Action.Types.after)
        if type_ not in acceptable_types:
            raise ValueError(
                f"cannot add state action with type \"{type_}\": must be {_summarize(acceptable_types, conjunction='or', quote=True)}", )
        action = Action(callable=callable, type=type_)
        super()._add_action(action)
        return action

    def remove_action(self, action: Action) -> Action:
        """Remove a action from the state."""
        return super()._remove_action(action)

    def remove_actions(
        self, actions: Union[None, List[Action], Literal[Action.Types.entry, Action.Types.exit]] = None,
    ) -> None:
        """Remove actions that are attached to the state.

        There are three modes of operation:
        - Passing a value of `None` (the default) will remove all actions.
        - Passing a specific set of `Action` objects will remove only those actions.
        - Passing `Action.Types.enter` or `Action.Types.exit` will remove all actions that match the given type.
        """
        return super()._remove_actions(actions)

    @property
    def states(self) -> Set['State']:
        """Return a set of all states referenced by the event."""
        return (set(self.sources) | {self.target}) - {None}

    def __hash__(self):
        return hash(self.name)


class StateMachine(pydantic.BaseModel):
    """StateMachine objects model state machines comprised of states, events,
    and associated actions.

    Initial state can be established via the `state` argument to the initializer but will not trigger
    any actions, as object initialization is run synchronously. If your state machine has actions on
    the initial state or entry events, initialize the state machine into an indeterminate state and then
    call `enter_state` or `trigger` to establish initial state and call all associated actions.

    Args:
        states: A list of states to add to the state machine.
        events: A list of events to add to the state machine.
        state: The initial state of the state machine. When `None` the state machine initializes into an
            indeterminate state. The `enter_state` and `trigger` methods can be used to establish an initial
            state post-initialization.
    """
    __state__: Optional[StateEnum] = None

    _state: Optional[State] = pydantic.PrivateAttr(None)
    _states: List[State] = pydantic.PrivateAttr([])
    _events: List[Event] = pydantic.PrivateAttr([])

    def __init__(
        self, states: List[State] = [], events: List[Event] = [],
        state: Optional[Union[State, str, StateEnum]] = None,
    ) -> None:
        super().__init__()

        # Initialize private attributes
        self._states.extend(states)
        self._events.extend(events)

        # Handle embedded States class
        state_enum = getattr(self.__class__, 'States', None)
        if state_enum:
            if not issubclass(state_enum, StateEnum):
                raise TypeError('States class must be a subclass of StateEnum')
            self._states.extend(State.from_enum(state_enum))

            # Adopt the initial state from the enum
            state = state if state else state_enum.__initial__

        # Handle type hints from __state__
        if not state_enum:
            type_hints = typing.get_type_hints(self.__class__)
            state_hint = type_hints['__state__']
            if inspect.isclass(state_hint) and issubclass(state_hint, StateEnum):
                self._states.extend(State.from_enum(state_hint))
            else:
                # Introspect the type hint
                type_origin = typing.get_origin(state_hint)
                if type_origin is typing.Union:
                    args = typing.get_args(state_hint)

                    for arg in args:
                        if inspect.isclass(arg) and issubclass(arg, StateEnum):
                            self._states.extend(State.from_enum(arg))
                else:
                    raise TypeError(f"unsupported type hint: \"{state_hint}\"")

        # Initial state
        if isinstance(state, State):
            if state not in self._states:
                raise ValueError(f'invalid initial state: the state object given is not in the state machine')
            self._state = state

        elif isinstance(state, (StateEnum, str)):
            state_ = self.get_state(state)
            if not state_:
                raise LookupError(f"invalid initial state: no state was found with the name \"{state}\"")
            self._state = state_

        elif state is None:
            # Assign from __state__ attribute if defined
            if initial_state := getattr(self.__class__, '__state__', None):
                state_ = self.get_state(initial_state)
                if not state_:
                    raise LookupError(f"invalid initial state: no state was found with the name \"{initial_state}\"")
                self._state = state_

        else:
            raise TypeError(f"invalid initial state: unexpected value of type \"{state.__class__.__name__}\": {state}")

        # Initialize any decorated methods
        for name, method in self.__class__.__dict__.items():
            if descriptor := getattr(method, '__event_descriptor__', None):
                if State.active() == descriptor.target:
                    target = State.active()
                else:
                    target = self.get_state(descriptor.target)
                    if not target:
                        raise ValueError(
                            f"event creation failed: target state \"{descriptor.target}\" is not in the state machine",
                        )

                source_names = list(filter(lambda s: s is not None, descriptor.source))
                sources = self.get_states(*source_names)
                if None in descriptor.source:
                    sources.append(None)

                event = Event(
                    name=method.__name__,
                    description=descriptor.description,
                    sources=sources,
                    target=target,
                    transition_type=descriptor.transition_type,
                )

                # Create bound methods and attach them as actions
                for type_ in Action.Types:
                    if not hasattr(descriptor, type_.name):
                        continue

                    callables = getattr(descriptor, type_.name)
                    for callable in callables:
                        event.add_action(
                            types.MethodType(callable, self),
                            type_,
                        )
                        self.add_event(event)

            elif descriptor := getattr(method, '__action_descriptor__', None):
                if descriptor.model == State:
                    obj = self.get_state(descriptor.name)
                    if not obj:
                        raise LookupError(f"unknown state: \"{descriptor.name}\"")
                elif descriptor.model == Event:
                    obj = self.get_event(descriptor.name)
                    if not obj:
                        raise LookupError(f"unknown event: \"{descriptor.name}\"")
                else:
                    raise TypeError(f'unknown model type: {descriptor.model.__name__}')

                # Create a bound method and attach the action
                obj.add_action(
                    types.MethodType(descriptor.callable, self),
                    descriptor.type,
                )

    @classmethod
    async def create(
        cls,
        states: List[State] = [],
        events: List[Event] = [],
        state: Optional[Union[State, str, StateEnum]] = None,
        *args,
        **kwargs
    ) -> 'StateMachine':
        """Asynchronously create a state machine and return it in the initial
        state.

        Actions are executed and arbitrary parameters can be supplied
        just as in the `enter_state` method.
        """
        state_machine = cls(states=states, events=events)
        state_ = state or state_machine.state
        if state_:
            if not await state_machine.enter_state(state_, *args, **kwargs):
                raise RuntimeError(f'failed creation of state machine: could not enter the requested state')
        return state_machine

    @property
    def state(self) -> Optional[State]:
        """Return the current state of the state machine."""
        return self._state

    @property
    def states(self) -> List[State]:
        """Return the list of states in the state machine."""
        return self._states.copy()

    def add_state(self, state: State) -> None:
        """Add a state to the state machine."""
        if self.get_state(state.name):
            raise ValueError(f"a state named \"{state.name}\" already exists")
        self._states.append(state)

    def add_states(self, states: Sequence[State]) -> None:
        """Add a sequence of states to the state machine."""
        [self.add_state(state) for state in states]

    def remove_state(self, state: State) -> None:
        """Remove a state from the state machine.

        Removing a state implicitly removes all events that reference
        the state as a source or target.
        """
        events = list(filter(lambda event: state in event.states, self.events))
        self.remove_events(events)
        self._states.remove(state)

    def remove_states(self, states: Sequence[State]) -> None:
        """Remove a sequence of states from the state machine."""
        [self.remove_state(state) for state in states]

    def get_state(self, name: Union[str, StateEnum]) -> Optional[State]:
        """Retrieve a state object by name or enum value."""
        name_ = name.name if isinstance(name, StateEnum) else name
        return next(filter(lambda s: s.name == name_, self.states), None)

    def get_states(self, *names: List[Union[str, StateEnum]]) -> List[State]:
        """Retrieve a list of states in the state machine by name or enum
        value."""
        names_ = []
        for name in names:
            if inspect.isclass(name) and issubclass(name, StateEnum):
                names_.extend(list(map(lambda i: i.name, name)))
            elif isinstance(name, (StateEnum, str)):
                name_ = name.name if isinstance(name, StateEnum) else name
                names_.append(name_)
            else:
                raise TypeError(f"cannot get state for type \"{name.__class__.__name__}\": {name}")

        return list(filter(lambda s: s.name in names_, self.states))

    @property
    def events(self) -> List[Event]:
        """Return the list of events in the state machine."""
        return self._events.copy()

    def add_event(self, event: Event) -> None:
        """Add an event to the state machine."""
        if self.get_event(event.name):
            raise ValueError(f"an event named \"{event.name}\" already exists")

        if missing := event.states - set(self.states):
            names = _summarize(list(map(lambda s: s.name, missing)), quote=True)
            raise ValueError(f'cannot add an event that references unknown states: {names}')
        self._events.append(event)

    def add_events(self, events: Sequence[Event]) -> None:
        """Add a list of events to the state machine."""
        [self.add_event(event) for event in events]

    def remove_event(self, event: Event) -> None:
        """Remove an event from the state machine."""
        self._events.remove(event)

    def remove_events(self, events: Sequence[Event]) -> None:
        """Remove a sequence of event from the state machine."""
        [self.remove_event(event) for event in events]

    def get_event(self, name: Union[str, StateEnum]) -> Optional[Event]:
        """Return the event with the given name or None if the state cannot be
        found."""
        if isinstance(name, (str, StateEnum)):
            name_ = name.name if isinstance(name, StateEnum) else name
        else:
            raise TypeError(f"cannot get event for name of type \"{name.__class__.__name__}\": {name}")

        return next(filter(lambda e: e.name == name_, self._events), None)

    async def trigger(self, event: Union[Event, str], *args, **kwargs) -> bool:
        """Trigger a state transition event.

        The state machine must be in a source state of the event being triggered. Initial event transitions
        can be triggered for events that have included `None` in their source states list.

        Args:
            event: The event to trigger a state transition with.
            args: Supplemental positional arguments to be passed to the transition and triggered actions.
            kwargs: Supplemental keyword arguments to be passed to the transition and triggered actions.

        Returns:
            A boolean value indicating if the transition was successful.

        Raises:
            ValueError: Raised if the event object is not a part of the state machine.
            LookupError: Raised if the event cannot be found by name.
            TypeError: Raised if the event value given is not an Event or str object.
        """
        if isinstance(event, Event):
            event_ = event
            if event_ not in self._events:
                raise ValueError(f'event trigger failed: the event object given is not in the state machine')

        elif isinstance(event, str):
            event_ = self.get_event(event)
            if not event_:
                raise LookupError(f"event trigger failed: no event was found with the name \"{event}\"")

        else:
            raise TypeError(
                f"event trigger failed: cannot trigger an event of type \"{event.__class__.__name__}\": {event}",
            )

        if self.state not in event_.sources:
            if self.state:
                raise RuntimeError(
                    f"event trigger failed: the \"{event_.name}\" event cannot be triggered from the current state of \"{self.state.name}\"", )
            else:
                raise RuntimeError(
                    f"event trigger failed: the \"{event_.name}\" event does not support initial state transitions",
                )

        # Substitute the active state if necessary
        target = self.state if event_.target == State.active() else event_.target
        if self.state is None and target is None:
            raise RuntimeError(f'event trigger failed: cannot transition from a None state to another None state')
        transition = Transition(state_machine=self, event=event_, source=self.state, target=target)
        return await transition(*args, **kwargs)

    async def enter_state(self, state: Union[State, StateEnum, str], *args, type_: Optional[Transition.Types] = None, **kwargs) -> bool:
        """Transition the state machine into a specific state.

        This method can be used to establish an initial state as an alternative to the object initializer,
        which cannot run actions as it is not a coroutine.

        When a state is entered, a transition is performed to change the current state into the given target state.
        By default, the type of transition performed is inferred based on the current state of the state machine.
        If the current state and the desired state differ, an external state transition is executed. If they are the same,
        then a self state transition is executed. Self state transitions will exit and reenter the current state, triggering
        all associated actions. The type of transition performed can be overridden via the `type_` argument.

        Entering a state directly via this method is typically only used to programmatically establish an initial state.
        Events should be favored over state entry unless you have very specific motivations as transitioning from one state
        to another in this manner can lead to inconsistent and surprising behavior because you may be forcing the state machine
        to change states in a way that is otherwise unreachable.

        # TODO: Forbid via a config class attribute?

        Args:
            state: The state to enter.
            args: Supplemental positional arguments to be passed to the transition and triggered actions.
            type_: The type of Transition to perform. When `None`, the type is inferred.
            kwargs: Supplemental keyword arguments to be passed to the transition and triggered actions.

        Returns:
            A boolean value indicating if the transition was successful.

        Raises:
            ValueError: Raised if the state object is not a part of the state machine.
            LookupError: Raised if the state cannot be found by name or enum value.
            TypeError: Raised if the state value given is not a State, StateEnum, or str object.
        """
        if isinstance(state, State):
            state_ = state
            if state_ not in self._states:
                raise ValueError(f'state entry failed: the state object given is not in the state machine')
        elif isinstance(state, (StateEnum, str)):
            name = state.name if isinstance(state, StateEnum) else state
            state_ = self.get_state(name)
            if not state_:
                raise LookupError(f"state entry failed: no state was found with the name \"{name}\"")
        else:
            raise TypeError(f"state entry failed: unexpected value of type \"{state.__class__.__name__}\": {state}")

        # Infer the transition type.
        type_ = type_ or (Transition.Types.self if self.state == state_ else Transition.Types.external)
        transition = Transition(state_machine=self, source=self.state, target=state_, type=type_)
        return await transition(*args, **kwargs)

    ##
    # Actions

    async def guard_transition(self, transition: 'Transition', *args, **kwargs) -> bool:
        """Guard the execution of every transition in the state machine.

        Guard actions can cancel the execution of transitions by returning `False` or
        raising an `AssertionError`.

        This method is provided for subclasses to override.

        Args:
            transition: The transition being applied to the state machine.
            args: A list of supplemental positional arguments passed when the transition was triggered.
            kwargs: A dict of supplemental keyword arguments passed when the transition was triggered.

        Returns:
            A boolean that indicates if the transition should be allowed to proceed.

        Raises:
            AssertionError: Raised to fail the guard exceptionally
        """
        return True

    async def before_transition(self, transition: 'Transition', *args, **kwargs) -> None:
        """Run before every transition in the state machine.

        This method is provided for subclasses to override.

        Args:
            transition: The transition being applied to the state machine.
            args: A list of supplemental positional arguments passed when the transition was triggered.
            kwargs: A dict of supplemental keyword arguments passed when the transition was triggered.
        """

    async def on_transition(self, transition: 'Transition', *args, **kwargs) -> None:
        """Run on every state change in the state machine.

        On actions are run at the moment that the state changes within the state machine,
        before any action defined on the states and event involved in the transition.

        This method is provided for subclasses to override.

        Args:
            transition: The transition being applied to the state machine.
            args: A list of supplemental positional arguments passed when the transition was triggered.
            kwargs: A dict of supplemental keyword arguments passed when the transition was triggered.
        """

    async def after_transition(self, transition: 'Transition', *args, **kwargs) -> None:
        """Run after every transition in the state machine.

        This method is provided for subclasses to override.

        Args:
            transition: The transition being applied to the state machine.
            args: A list of supplemental positional arguments passed when the transition was triggered.
            kwargs: A dict of supplemental keyword arguments passed when the transition was triggered.
        """

    def __repr_args__(self) -> pydantic.ReprArgs:
        return [('states', self.states), ('events', self.events), ('state', self.state)]

    class Config:
        allow_entry = 'initial'  # TODO: any, forbid, accept...
        guard_with = 'exception'  # TODO: warning, silence


class Transition(pydantic.BaseModel):
    """Transition objects model a state change within a state machine.

    The behavior of a transition is dependent upon the current state of the state machine, the source and target
    states involved in the transition, the event (if any) that triggered the transition, and the type of transition
    that is occurring. See the documentation of the `Transition.Types` class for specifics about the types of transitions
    and how they behave.

    Args:
        state_machine: The state machine in which the transition is occurring.
        source: The state that the state machine is transitioning from. `None` indicates an initial state transition.
        target: The state that the state machine is transition to.
        event: The event (if any) that triggered the state transition.
        type: The type of transition to perform.

    Attributes:
        state_machine: The state machine in which the transition is occurring.
        source: The state of the state machine when the transition started. None indicates an initial state.
        target: The state that the state machine will be in once the transition has finished.
        event: The event that triggered the transition. None indicates that the state was entered directly.
        type: The type of transition occurring (internal, external, or self).
        created_at: When the transition was created.
        started_at: When the transition started. None if the transition has not been called.
        finished_at: When the transition finished. None if the transition has not been called or is underway.
        cancelled: Whether or not the transition was cancelled by a guard callback or action. None if the transition has not been called or is underway.
        args: Supplemental positional arguments passed to the transition when it was called.
        kwargs: Supplemental keyword arguments passed to the transition when it was called.
    """
    class Types(enum.Enum):
        """An enumeration that describes the type of state transition that is
        occurring.

        External transitions are the most common type in which the state of the state machine is moved from one state to another.
        Internal and self transitions occur when the source and target states are the same. In an internal transition, the state
        is not changed and will not trigger associated exit and entry actions. In a self transition, the state is exited and
        reentered and will trigger associated exit and entry actions.

        Internal transitions are commonly used in situations where maintaining the status quo is uninteresting or insignificant in
        and of itself. Self transitions are used in situations where the transition into the same state represents something meaningful
        or interesting.

        Attributes:
            external: A transition in which the state is changed from one value to another.
            internal: A transition in which the source and target states are the same but are not exited and reentered during the transition.
            self: A transition in which the source and target states are the same and are exited and reentered during the transition.
        """
        external = 'External Transition'
        internal = 'Internal Transition'
        self = 'Self Transition'

    state_machine: StateMachine
    source: Optional[State] = None
    target: State
    event: Optional[Event] = None
    type: Transition.Types
    created_at: datetime.datetime = pydantic.Field(default_factory=datetime.datetime.now)
    started_at: Optional[datetime.datetime] = None
    finished_at: Optional[datetime.datetime] = None
    cancelled: Optional[bool] = None
    args: Optional[List[Any]] = None
    kwargs: Optional[Dict[str, Any]] = None

    def __init__(self, state_machine: StateMachine, *args, **kwargs) -> None:
        super().__init__(state_machine=state_machine, *args, **kwargs)
        self.state_machine = state_machine  # Ensure we have a reference and not a copy (Pydantic behavior)

    async def __call__(self, *args, **kwargs) -> bool:
        """Execute the transition."""
        if self.started_at:
            raise RuntimeError(f'transition has already been executed')

        self.args = args
        self.kwargs = kwargs

        async with self._lifecycle():
            # Guards can cancel the transition via return value or failed assertion
            self.cancelled = False
            try:
                if not await _call_with_matching_parameters(self.state_machine.guard_transition, self, *args, **kwargs):
                    raise AssertionError(f'transition cancelled by guard_transition callback')
            except AssertionError:
                self.cancelled = True
                return False
            await _call_with_matching_parameters(self.state_machine.before_transition, self, *args, **kwargs)

            try:
                results = await self._run_actions(self.event, Action.Types.guard)
                success = (
                    functools.reduce(lambda x, y: x and y, results, True) if results
                    else True
                )
                if not success:
                    raise AssertionError(f'transition cancelled by guard action')
            except AssertionError:
                self.cancelled = True
                return False
            await self._run_actions(self.event, Action.Types.before)

            # Switch between states and try to stay consistent. Actions can be lost in failures
            try:
                if self.type in (Transition.Types.external, Transition.Types.self):
                    await self._run_actions(self.source, Action.Types.exit)

                self.state_machine._state = self.target
                await _call_with_matching_parameters(self.state_machine.on_transition, self, *args, **kwargs)
                await self._run_actions(self.event, Action.Types.on)

                if self.type in (Transition.Types.external, Transition.Types.self):
                    await self._run_actions(self.target, Action.Types.entry)

            except Exception:
                self.state_machine._state = self.source
                raise

            await self._run_actions(self.event, Action.Types.after)
            await _call_with_matching_parameters(self.state_machine.after_transition, self, *args, **kwargs)

            return True

    @pydantic.root_validator(pre=True)
    @classmethod
    def _set_default_type_from_event(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        type_ = values.get('type', None)
        event = values.get('event', None)

        if type_ is None and event is not None:
            type_ = event.transition_type or Transition.Types.external
        else:
            type_ = Transition.Types.external

        values.setdefault('type', type_)

        return values

    @pydantic.root_validator()
    @classmethod
    def _validate_type(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        type_ = values['type']
        if type_ in (Transition.Types.internal, Transition.Types.self):
            assert values['target'] == values['source'], 'source and target states must be the same for internal or self transitions'
        elif type_ == Transition.Types.external:
            assert values['target'] != values['source'], 'source and target states cannot be the same for external transitions'
        else:
            raise ValueError(f"unknown transition type: \"{type_}\"")

        return values

    @property
    def is_executing(self) -> bool:
        """Return a boolean value that indicates if the transition is in
        progress."""
        return self.started_at is not None and self.finished_at is None

    @property
    def is_finished(self) -> bool:
        """Return a boolean value that indicates if the transition has
        finished."""
        return self.started_at is not None and self.finished_at is not None

    @property
    def runtime(self) -> Optional[datetime.timedelta]:
        """Return a time delta value detailing how long the transition took to
        execute."""
        return self.finished_at - self.started_at if self.is_finished else None

    @contextlib.asynccontextmanager
    async def _lifecycle(self):
        """Manage lifecycle context for transition execution."""
        try:
            self.started_at = datetime.datetime.now()
            yield

        finally:
            self.finished_at = datetime.datetime.now()

    async def _run_actions(self, model: Optional[BaseModel], type_: Action.Types) -> Optional[List[Any]]:
        """Run all the actions of a given type attached to a State or Event
        model.

        Returns:
            An aggregated list of return values from the actions run or None if the model is None.
        """
        return await model._run_actions(type_, transition=self, *self.args, **self.kwargs) if model else None


Transition.update_forward_refs()
Event.update_forward_refs()

StateIdentifier = Union[StateEnum, str]
Source = Union[None, StateIdentifier, List[StateIdentifier], Type[StateEnum]]
Target = Union[None, StateIdentifier, ActiveState]


class EventDescriptor(pydantic.BaseModel):
    description: Optional[str] = None
    transition_type: Optional[Transition.Types] = None
    source: List[Union[None, StateIdentifier]]
    target: Target
    guard: List[Callable]
    before: List[Callable]
    on: List[Callable]
    after: List[Callable]

    @pydantic.validator('source', pre=True)
    @classmethod
    def _listify_sources(cls, value: Source) -> List[Union[None, StateIdentifier]]:
        identifiers = []

        if isinstance(value, list):
            identifiers.extend(value)
        else:
            identifiers.append(value)

        return identifiers

    @pydantic.validator('source', each_item=True, pre=True)
    def _map_enums(cls, v) -> Optional[str]:
        if isinstance(v, StateEnum):
            return v.name

        return v

    @pydantic.validator('guard', 'before', 'on', 'after', pre=True)
    @classmethod
    def _listify_actions(cls, value: Union[None, Callable, List[Callable]]) -> List[Callable]:
        callables = []

        if value is None:
            pass
        elif isinstance(value, Callable):
            callables.append(value)
        elif isinstance(value, list):
            callables.extend(value)

        return callables

    def __hash__(self):
        return hash(self.name)


class ActionDescriptor(pydantic.BaseModel):
    """Describes an action attached to a State or Event."""
    model: Type[BaseModel]
    name: str
    description: Optional[str] = None
    type: Action.Types
    callable: Callable


def event(
    description: Optional[str],
    source: Source,
    target: Target,
    *,
    guard: Union[None, Callable, List[Callable]] = None,
    before: Union[None, Callable, List[Callable]] = None,
    after: Union[None, Callable, List[Callable]] = None,
    transition_type: Optional[Transition.Types] = None,
    **kwargs
) -> None:
    """Transform a method into a state machine event.

    The original method is attached to the newly created event as an on action.

    The decorated function must be a method on a subclass of `StateMachine`.

    Args:
        description: An optional description of the event.
        source: The state or states that the event can be triggered from. `None` indicates an initial state transition.
        target: The state that the state machine will transition into at the completion of the transition.
        guard: An optional list of callables to attach as guard actions to the newly created event.
        before: An optional list of callables to attach as before actions to the newly created event.
        after: An optional list of callables to attach as after actions to the newly created event.
        transition_type: The type of transition to perform when the event is triggered. When `None`, the type is inferred.
    """
    def decorator(fn):
        target_ = target.name if isinstance(target, StateEnum) else target
        descriptor = EventDescriptor(
            description=description,
            source=source,
            target=target_,
            transition_type=transition_type,
            guard=guard,
            before=before,
            after=after,
            on=fn,
        )

        @functools.wraps(fn)
        async def event_trigger(self, *args, **kwargs) -> bool:
            # NOTE: The original function is attached as an on event handler
            return await self.trigger(fn.__name__, *args, **kwargs)

        event_trigger.__event_descriptor__ = descriptor
        return event_trigger

    return decorator


def enter_state(name: Union[str, StateEnum], description: str = '') -> None:
    """Transform a method into an enter state action."""
    return _state_action(name, Action.Types.entry, description)


def exit_state(name: Union[str, StateEnum], description: str = '') -> None:
    """Transform a method into an exit state action."""
    return _state_action(name, Action.Types.exit, description)


def _state_action(name: Union[str, StateEnum], type_: Action.Types, description: str = ''):
    def decorator(fn):
        name_ = name.name if isinstance(name, StateEnum) else name
        descriptor = ActionDescriptor(
            model=State,
            name=name_,
            description=description,
            type=type_,
            callable=fn,
        )

        fn.__action_descriptor__ = descriptor
        return fn

    return decorator


def guard_event(name: str, description: str = '') -> None:
    """Transform a method into a guard event action."""
    return _event_action(name, Action.Types.guard, description)


def before_event(name: str, description: str = '') -> None:
    """Transform a method into a before event action."""
    return _event_action(name, Action.Types.before, description)


def on_event(name: str, description: str = '') -> None:
    """Transform a method into an on event action."""
    return _event_action(name, Action.Types.on, description)


def after_event(name: str, description: str = '') -> None:
    """Transform a method into an after event action."""
    return _event_action(name, Action.Types.after, description)


def _event_action(name: str, type_: Action.Types, description: str = ''):
    def decorator(fn):
        descriptor = ActionDescriptor(
            model=Event,
            name=name,
            description=description,
            type=type_,
            callable=fn,
        )

        fn.__action_descriptor__ = descriptor
        return fn

    return decorator


def _summarize(
    values: Sequence[str], *, conjunction: str = 'and', quote=False, oxford_comma: bool = True
) -> str:
    """Concatenate a sequence of strings into a series suitable for use in
    English output.

    Items are joined using a comma and a configurable conjunction,
    defaulting to 'and'.
    """
    count = len(values)
    values = _quote(values) if quote else values
    if count == 0:
        return ''
    elif count == 1:
        return values[0]
    elif count == 2:
        return f' {conjunction} '.join(values)
    else:
        series = ', '.join(values[0:-1])
        last_item = values[-1]
        delimiter = ',' if oxford_comma else ''
        return f'{series}{delimiter} {conjunction} {last_item}'


def _quote(values: Sequence[str]) -> List[str]:
    """Return a sequence of strings surrounding each value in double quotes."""
    return list(map(lambda v: f"\"{v}\"", values))


def _parameters_matching_signature(signature: inspect.Signature, *args, **kwargs) -> Tuple[List[Any], Dict[str, Any]]:
    """Return a tuple of positional and keyword parameters that match a
    callable signature.

    This function reduces input parameters down to the subset that
    matches the given signature. It supports callback based APIs by
    allowing each callback to opt into the parameters of interest by
    including them in the function signature. The matching subset of
    parameters returned may be insufficient for satisfying the signature
    but will not contain extraneous non-matching parameters.
    """
    parameters: Mapping[
        str, inspect.Parameter,
    ] = dict(
        filter(
            lambda item: item[0] not in {'self', 'cls'},
            signature.parameters.items(),
        ),
    )

    args_copy, kwargs_copy = collections.deque(args), kwargs.copy()
    matched_args, matched_kwargs = [], {}
    for name, parameter in parameters.items():
        if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
            # positional only: must be dequeued args
            if len(args_copy):
                matched_args.append(args_copy.popleft())

        elif parameter.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD:
            # positional or keyword: check kwargs first and then dequeue from args
            if name in kwargs_copy:
                matched_kwargs[name] = kwargs_copy.pop(name)
                if len(args_copy):
                    # pop the positional arg we have consumed via keyword match
                    args_copy.popleft()
            elif len(args_copy):
                matched_args.append(args_copy.popleft())

        elif parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            matched_args.extend(args_copy)
            args_copy.clear()

        elif parameter.kind == inspect.Parameter.KEYWORD_ONLY:
            if name in kwargs_copy:
                matched_kwargs[name] = kwargs_copy.pop(name)

        elif parameter.kind == inspect.Parameter.VAR_KEYWORD:
            matched_kwargs.update(kwargs_copy)
            kwargs_copy.clear()

    return matched_args, matched_kwargs


async def _call_with_matching_parameters(callable: Callable, *args, **kwargs) -> Any:
    """Call a callable with all parameters that match its signature and return
    the results."""
    matched_args, matched_kwargs = _parameters_matching_signature(
        inspect.Signature.from_callable(callable), *args, **kwargs
    )
    if asyncio.iscoroutinefunction(callable):
        return await callable(*matched_args, **matched_kwargs)
    else:
        return callable(*matched_args, **matched_kwargs)


class HistoryMixin(pydantic.BaseModel):
    """A mixin that records all transitions that occur within the state
    machine."""
    __private_attributes__ = {'_transitions': pydantic.PrivateAttr([])}

    async def after_transition(self, transition: Transition) -> None:
        """Append a completed transition to the history."""
        self._transitions.append(transition)

    @property
    def history(self) -> List[Transition]:
        """Return a list of historical transitions that have occurred within
        the state machine."""
        return self._transitions.copy()

    def clear_history(self) -> None:
        """Clear the history of recorded transitions."""
        self._transitions.clear()

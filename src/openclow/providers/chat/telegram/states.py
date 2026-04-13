"""FSM states for Telegram bot conversation flows."""
from aiogram.fsm.state import State, StatesGroup


class TaskStates(StatesGroup):
    choosing_project = State()
    entering_description = State()
    confirming = State()

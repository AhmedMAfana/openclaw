import { createContext, useContext } from "react";

export type TaskMode = "quick" | "plan";

export interface TaskModeState {
  mode: TaskMode;
  setMode: (mode: TaskMode) => void;
}

export const TaskModeContext = createContext<TaskModeState>({
  mode: "quick",
  setMode: () => {},
});

export const useTaskMode = () => useContext(TaskModeContext);

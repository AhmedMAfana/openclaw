import { createContext, useContext } from "react";

export interface ThinkingState {
  steps: string[];
}

export const ThinkingContext = createContext<ThinkingState>({ steps: [] });
export const useThinking = () => useContext(ThinkingContext);

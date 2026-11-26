import React, { createContext, useContext, useState, useEffect, ReactNode } from 'react';
import { applyMode, Mode } from '@cloudscape-design/global-styles';

interface ThemeContextType {
  mode: 'light' | 'dark';
  toggleMode: () => void;
}

const ThemeContext = createContext<ThemeContextType | undefined>(undefined);

export const ThemeProvider: React.FC<{ children: ReactNode }> = ({ children }) => {
  const [mode, setMode] = useState<'light' | 'dark'>(() => {
    const saved = localStorage.getItem('theme-mode');
    return (saved === 'dark') ? 'dark' : 'light';
  });

  useEffect(() => {
    applyMode(mode === 'dark' ? Mode.Dark : Mode.Light);
    localStorage.setItem('theme-mode', mode);
  }, [mode]);

  const toggleMode = () => setMode((prev) => (prev === 'light' ? 'dark' : 'light'));

  return (
    <ThemeContext.Provider value={{ mode, toggleMode }}>
      {children}
    </ThemeContext.Provider>
  );
};

export const useTheme = () => {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error('useTheme must be inside ThemeProvider');
  return ctx;
};

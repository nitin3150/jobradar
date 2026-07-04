import { createContext, useContext, useEffect, useState } from 'react';

const STORAGE_KEY = 'jobradar:category';
const CategoryContext = createContext(null);

export function CategoryProvider({ children }) {
  const [category, setCategory] = useState(() => {
    try {
      return window.localStorage.getItem(STORAGE_KEY) || 'startup';
    } catch {
      return 'startup';
    }
  });

  useEffect(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, category);
    } catch {
      /* localStorage disabled — ignore */
    }
  }, [category]);

  return (
    <CategoryContext.Provider value={{ category, setCategory }}>
      {children}
    </CategoryContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useCategory() {
  const ctx = useContext(CategoryContext);
  if (!ctx) throw new Error('useCategory must be used inside <CategoryProvider>');
  return ctx;
}

import { createContext, useContext, useEffect, useState } from 'react';
import { CATEGORIES, DEFAULT_CATEGORY } from './categories';

const STORAGE_KEY = 'jobradar:category';

// Re-export so existing imports keep working; the authoritative list lives
// in `./categories` to keep this file a pure component module (fast-refresh safe).
export { CATEGORIES, DEFAULT_CATEGORY };

const CategoryContext = createContext(null);

export function CategoryProvider({ children }) {
  const [category, setCategory] = useState(() => {
    try {
      const stored = window.localStorage.getItem(STORAGE_KEY);
      return CATEGORIES.includes(stored) ? stored : DEFAULT_CATEGORY;
    } catch {
      return DEFAULT_CATEGORY;
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

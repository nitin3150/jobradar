// Five scanner domains — one per backend endpoint.
// Kept in their own module so fast-refresh can pick up changes without
// having to disable the react-refresh export rule in CategoryContext.jsx.
export const CATEGORIES = ['funding', 'ngos', 'remote', 'boards', 'oss'];
export const DEFAULT_CATEGORY = 'boards';

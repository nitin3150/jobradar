import { useCallback } from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import Navbar from './components/Navbar';
import Dashboard from './pages/Dashboard';
import CompanyDetail from './pages/CompanyDetail';
import JobBoard from './pages/JobBoard';
import JobDetail from './pages/JobDetail';
import ApplicationTracker from './pages/ApplicationTracker';
import QABank from './pages/QABank';
import { CategoryProvider, useCategory } from './contexts/CategoryContext';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

function Shell() {
  const { category, setCategory } = useCategory();

  // Reset to page 1 whenever the user changes category. Kept here so it works
  // whether the change came from the navbar or any future in-page control.
  const handleCategoryChange = useCallback(
    (next) => {
      setCategory(next);
    },
    [setCategory],
  );

  return (
    <div className="min-h-screen bg-gray-50">
      <Navbar category={category} onCategoryChange={handleCategoryChange} />
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/company/:id" element={<CompanyDetail />} />
        <Route path="/jobs" element={<JobBoard />} />
        <Route path="/jobs/:id" element={<JobDetail />} />
        <Route path="/applications" element={<ApplicationTracker />} />
        <Route path="/qa-bank" element={<QABank />} />
      </Routes>
    </div>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <CategoryProvider>
          <Shell />
        </CategoryProvider>
      </BrowserRouter>
    </QueryClientProvider>
  );
}

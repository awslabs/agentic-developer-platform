import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { AuthProvider } from '@/contexts/AuthContext';
import { OnboardingView } from '@superplane-ui/OnboardingView';
import '@/index.css';

createRoot(document.getElementById('root')!).render(
  <BrowserRouter><AuthProvider><OnboardingView /></AuthProvider></BrowserRouter>,
);

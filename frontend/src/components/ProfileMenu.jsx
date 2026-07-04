import { useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useClickOutside } from '../hooks/useClickOutside';
import ResumesModal from './modals/ResumesModal';
import PreferencesModal from './modals/PreferencesModal';
import SchedulerModal from './modals/SchedulerModal';

const MENU_ID = 'profile-menu';
const MENU_TITLE_ID = 'profile-menu-title';

function UserAvatar() {
  return (
    <span className="absolute inset-0 rounded-full bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center text-white font-semibold text-sm shadow-inner">
      U
    </span>
  );
}

const MENU_ITEMS = [
  { key: 'qa-bank', label: 'Q&A Bank', icon: 'library', description: 'Answers used during auto-apply' },
  { key: 'scheduler', label: 'Scheduler', icon: 'clock', description: 'How often to search & apply' },
  { key: 'resumes', label: 'Resumes', icon: 'doc', description: 'Upload & manage resume files' },
  { key: 'preferences', label: 'Preferences', icon: 'sliders', description: 'Roles, fit score, follow-ups' },
];

function MenuIcon({ name }) {
  const common = { className: 'w-5 h-5 shrink-0', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 2, strokeLinecap: 'round', strokeLinejoin: 'round' };
  switch (name) {
    case 'library':
      return (
        <svg {...common}><path d="M3 19V5a2 2 0 0 1 2-2h6l2 2h6a2 2 0 0 1 2 2v12H3z" /><path d="M3 19h18" /></svg>
      );
    case 'clock':
      return (
        <svg {...common}><circle cx="12" cy="12" r="9" /><polyline points="12 7 12 12 15 14" /></svg>
      );
    case 'doc':
      return (
        <svg {...common}><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /></svg>
      );
    case 'sliders':
      return (
        <svg {...common}><line x1="4" y1="21" x2="4" y2="14" /><line x1="4" y1="10" x2="4" y2="3" /><line x1="12" y1="21" x2="12" y2="12" /><line x1="12" y1="8" x2="12" y2="3" /><line x1="20" y1="21" x2="20" y2="16" /><line x1="20" y1="12" x2="20" y2="3" /><line x1="1" y1="14" x2="7" y2="14" /><line x1="9" y1="8" x2="15" y2="8" /><line x1="17" y1="16" x2="23" y2="16" /></svg>
      );
    default:
      return null;
  }
}

export default function ProfileMenu() {
  const containerRef = useRef(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [modal, setModal] = useState(null); // 'resumes' | 'preferences' | 'scheduler' | null
  const navigate = useNavigate();

  useClickOutside(containerRef, () => setMenuOpen(false), menuOpen);

  const openModal = (key) => {
    setMenuOpen(false);
    setModal(key);
  };

  const handleSelect = (key) => {
    if (key === 'qa-bank') {
      setMenuOpen(false);
      navigate('/qa-bank');
      return;
    }
    openModal(key);
  };

  return (
    <>
      <div ref={containerRef} className="relative">
        <button
          onClick={() => setMenuOpen((o) => !o)}
          aria-haspopup="menu"
          aria-controls={menuOpen ? MENU_ID : undefined}
          aria-expanded={menuOpen}
          aria-label="Open profile menu"
          className="relative w-9 h-9 rounded-full hover:ring-2 hover:ring-indigo-200 transition-shadow focus:outline-none focus:ring-2 focus:ring-indigo-300"
        >
          <UserAvatar />
        </button>

        {menuOpen && (
          <div
            id={MENU_ID}
            role="menu"
            aria-labelledby={MENU_TITLE_ID}
            className="absolute right-0 mt-2 w-72 bg-white border border-gray-200 rounded-xl shadow-xl overflow-hidden z-50 origin-top-right anim-fade"
          >
            <div className="px-4 py-3 border-b border-gray-100 bg-gradient-to-br from-indigo-50/80 to-violet-50/80">
              <h2 id={MENU_TITLE_ID} className="text-sm font-semibold text-gray-900">
                Your profile
              </h2>
              <div className="text-xs text-gray-500">Search · Apply · Track</div>
            </div>
            <ul className="py-1">
              {MENU_ITEMS.map((item) => (
                <li key={item.key}>
                  <button
                    role="menuitem"
                    onClick={() => handleSelect(item.key)}
                    className="w-full flex items-start gap-3 px-4 py-2.5 hover:bg-gray-50 transition-colors text-left"
                  >
                    <span className="mt-0.5 text-indigo-600">
                      <MenuIcon name={item.icon} />
                    </span>
                    <span className="flex-1 min-w-0">
                      <span className="block text-sm font-medium text-gray-900">{item.label}</span>
                      <span className="block text-xs text-gray-500">{item.description}</span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
            <div className="px-4 py-2 border-t border-gray-100 bg-gray-50 text-[11px] text-gray-400">
              Settings stay on this device until you sign in.
            </div>
          </div>
        )}
      </div>

      <ResumesModal open={modal === 'resumes'} onClose={() => setModal(null)} />
      <PreferencesModal open={modal === 'preferences'} onClose={() => setModal(null)} />
      <SchedulerModal open={modal === 'scheduler'} onClose={() => setModal(null)} />
    </>
  );
}

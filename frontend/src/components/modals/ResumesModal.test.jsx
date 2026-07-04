import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ResumesModal from './ResumesModal';

// Stub alert/confirm so the Remove button's confirm() doesn't block.
beforeEach(() => {
  vi.spyOn(window, 'confirm').mockImplementation(() => true);
});

// Mock the hook so we don't hit React Query's cache/network here — we want
// to focus on the modal's wiring (dropzone → upload, button → handlers).
const mockHook = {
  resumes: [],
  isLoading: false,
  uploadAsync: vi.fn(),
  isUploading: false,
  uploadError: null,
  remove: vi.fn(),
  isRemoving: false,
  update: vi.fn(),
  isUpdating: false,
};
vi.mock('../../hooks/useResumes', () => ({
  useResumes: () => mockHook,
  formatBytes: (b) => `${b}B`,
}));

const FIXTURE_RESUMES = [
  {
    id: 'a',
    name: 'ml-resume.pdf',
    content_type: 'application/pdf',
    size_bytes: 2048,
    tags: ['ml', 'engineer'],
    is_default: true,
    uploaded_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 'b',
    name: 'backend-resume.pdf',
    content_type: 'application/pdf',
    size_bytes: 4096,
    tags: ['backend'],
    is_default: false,
    uploaded_at: '2026-01-02T00:00:00Z',
  },
];

describe('ResumesModal (server-backed)', () => {
  beforeEach(() => {
    Object.assign(mockHook, {
      resumes: [],
      isLoading: false,
      uploadAsync: vi.fn(),
      isUploading: false,
      uploadError: null,
      remove: vi.fn(),
      isRemoving: false,
      update: vi.fn(),
      isUpdating: false,
    });
  });

  it('renders an empty-state message when no resumes are on file', () => {
    mockHook.resumes = [];
    render(<ResumesModal open onClose={() => {}} />);
    expect(screen.getByText('No resumes uploaded yet.')).toBeInTheDocument();
  });

  it('renders one row per resume with the default checkbox + remove button', () => {
    mockHook.resumes = FIXTURE_RESUMES;
    render(<ResumesModal open onClose={() => {}} />);
    expect(screen.getByText('ml-resume.pdf')).toBeInTheDocument();
    expect(screen.getByText('backend-resume.pdf')).toBeInTheDocument();
    const checkboxes = screen.getAllByRole('checkbox', { name: /Default/i });
    expect(checkboxes).toHaveLength(2);
    expect(checkboxes[0]).toBeChecked();
    expect(checkboxes[1]).not.toBeChecked();
    expect(screen.getAllByRole('button', { name: /Remove/i })).toHaveLength(2);
  });

  it('invokes uploadAsync when a file is selected via the hidden <input>', async () => {
    const file = new File(['hello'], 'cv.pdf', { type: 'application/pdf' });
    mockHook.uploadAsync.mockResolvedValue({ id: 'x' });
    render(<ResumesModal open onClose={() => {}} />);
    const input = document.querySelector('input[type="file"]');
    // userEvent uploads by assigning files programmatically.
    await userEvent.upload(input, file);
    await waitFor(() => expect(mockHook.uploadAsync).toHaveBeenCalledTimes(1));
    const call = mockHook.uploadAsync.mock.calls[0][0];
    expect(call.file).toBe(file);
    expect(typeof call.onProgress).toBe('function');
  });

  it('toggles the default flag through the row checkbox', () => {
    mockHook.resumes = FIXTURE_RESUMES;
    render(<ResumesModal open onClose={() => {}} />);
    const checkboxes = screen.getAllByRole('checkbox', { name: /Default/i });
    fireEvent.click(checkboxes[1]); // toggle the second resume to default
    expect(mockHook.update).toHaveBeenCalledWith({
      id: 'b',
      patch: { is_default: true },
    });
  });

  it('commits edited tags on blur', async () => {
    mockHook.resumes = FIXTURE_RESUMES;
    render(<ResumesModal open onClose={() => {}} />);
    const tagInput = screen.getAllByPlaceholderText(/tags/i)[0];
    await userEvent.clear(tagInput);
    await userEvent.type(tagInput, 'ml, llm, agent');
    fireEvent.blur(tagInput);
    expect(mockHook.update).toHaveBeenCalledWith({
      id: 'a',
      patch: { tags: ['ml', 'llm', 'agent'] },
    });
  });

  it('calls remove when the user confirms', () => {
    mockHook.resumes = FIXTURE_RESUMES;
    render(<ResumesModal open onClose={() => {}} />);
    fireEvent.click(screen.getAllByRole('button', { name: /Remove/i })[0]);
    expect(window.confirm).toHaveBeenCalled();
    expect(mockHook.remove).toHaveBeenCalledWith('a');
  });
});

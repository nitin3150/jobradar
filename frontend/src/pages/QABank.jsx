import { useState } from 'react';
import {
  useQABank,
  useUpdateQAEntry,
  useCreateQAEntry,
  useDeleteQAEntry,
} from '../hooks/useQABank';

function EntryRow({ entry, onSave, onDelete }) {
  const [editing, setEditing] = useState(false);
  const [answer, setAnswer] = useState(entry.answer || '');

  const handleSave = () => {
    onSave(entry.id, { answer });
    setEditing(false);
  };

  return (
    <tr
      className={`border-b border-gray-100 ${
        !entry.answer ? 'bg-orange-50' : 'hover:bg-gray-50'
      }`}
    >
      <td className="px-4 py-3">
        <div className="font-medium text-gray-900 text-sm">{entry.canonical_question}</div>
        <div className="text-xs text-gray-400 mt-0.5">{entry.question_pattern}</div>
      </td>
      <td className="px-4 py-3 text-xs text-gray-500">{entry.answer_type}</td>
      <td className="px-4 py-3">
        {editing ? (
          <div className="flex gap-2">
            <input
              value={answer}
              onChange={(e) => setAnswer(e.target.value)}
              className="flex-1 border border-gray-300 rounded px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              autoFocus
            />
            <button
              onClick={handleSave}
              className="px-3 py-1 bg-indigo-600 text-white text-xs rounded hover:bg-indigo-700"
            >
              Save
            </button>
            <button
              onClick={() => {
                setAnswer(entry.answer || '');
                setEditing(false);
              }}
              className="px-3 py-1 bg-gray-100 text-gray-700 text-xs rounded hover:bg-gray-200"
            >
              Cancel
            </button>
          </div>
        ) : (
          <button
            onClick={() => setEditing(true)}
            className={`text-sm px-2 py-1 rounded w-full text-left ${
              entry.answer
                ? 'text-gray-800 hover:bg-gray-100'
                : 'text-orange-500 hover:bg-orange-100'
            }`}
          >
            {entry.answer || '⚠ No answer — click to fill'}
          </button>
        )}
      </td>
      <td className="px-4 py-3 text-center text-xs text-gray-500">{entry.times_used}</td>
      <td className="px-4 py-3 text-center">
        <button
          onClick={() => onDelete(entry.id)}
          className="text-red-400 hover:text-red-600 text-xs"
        >
          Delete
        </button>
      </td>
    </tr>
  );
}

export default function QABank() {
  const { data: entries, isLoading } = useQABank();
  const updateEntry = useUpdateQAEntry();
  const createEntry = useCreateQAEntry();
  const deleteEntry = useDeleteQAEntry();
  const [showAdd, setShowAdd] = useState(false);
  const [newQuestion, setNewQuestion] = useState('');
  const [newAnswer, setNewAnswer] = useState('');

  const handleAdd = () => {
    if (!newQuestion.trim()) return;
    createEntry.mutate({
      question_pattern: newQuestion.toLowerCase().trim(),
      canonical_question: newQuestion.trim(),
      answer: newAnswer.trim() || null,
    });
    setNewQuestion('');
    setNewAnswer('');
    setShowAdd(false);
  };

  return (
    <div className="max-w-5xl mx-auto px-4 py-6">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-2xl font-bold text-gray-900">Q&A Bank</h1>
            <p className="text-sm text-gray-500 mt-1">
              Answers used to auto-fill job application forms.
              <span className="ml-2 text-orange-500 font-medium">
                Orange rows need your answer.
              </span>
            </p>
          </div>
          <button
            onClick={() => setShowAdd(true)}
            className="px-4 py-2 bg-indigo-600 text-white text-sm font-medium rounded-lg hover:bg-indigo-700"
          >
            + Add Entry
          </button>
        </div>

        {showAdd && (
          <div className="bg-white border border-indigo-200 rounded-xl p-4 mb-4 shadow-sm">
            <h3 className="font-medium text-gray-900 mb-3">New Entry</h3>
            <div className="flex gap-3">
              <input
                placeholder="Question (e.g. 'years of experience')"
                value={newQuestion}
                onChange={(e) => setNewQuestion(e.target.value)}
                className="flex-1 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              />
              <input
                placeholder="Your answer"
                value={newAnswer}
                onChange={(e) => setNewAnswer(e.target.value)}
                className="flex-1 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              />
              <button
                onClick={handleAdd}
                className="px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700"
              >
                Add
              </button>
              <button
                onClick={() => setShowAdd(false)}
                className="px-4 py-2 bg-gray-100 text-gray-700 text-sm rounded-lg hover:bg-gray-200"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {isLoading && (
          <div className="text-center py-12 text-gray-500">Loading...</div>
        )}

        <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Question</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600 w-24">Type</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Answer</th>
                <th className="text-center px-4 py-3 font-medium text-gray-600 w-20">
                  Used
                </th>
                <th className="w-16" />
              </tr>
            </thead>
            <tbody>
              {entries?.map((entry) => (
                <EntryRow
                  key={entry.id}
                  entry={entry}
                  onSave={(id, data) => updateEntry.mutate({ id, ...data })}
                  onDelete={(id) => deleteEntry.mutate(id)}
                />
              ))}
            </tbody>
          </table>
          {!isLoading && !entries?.length && (
            <div className="text-center py-12 text-gray-400">
              No entries yet. Add your first answer above.
            </div>
          )}
        </div>
    </div>
  );
}

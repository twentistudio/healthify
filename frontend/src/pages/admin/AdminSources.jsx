import { AlertCircle, ArrowLeft, BookOpen, LogOut, Plus, Search } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';

const BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api';

const AdminSources = () => {
    const navigate = useNavigate();
    const [sources, setSources] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [searchTerm, setSearchTerm] = useState('');
    const [page, setPage] = useState(1);
    const [pagination, setPagination] = useState({});
    const [showModal, setShowModal] = useState(false);
    const [editingSource, setEditingSource] = useState(null);
    const [formData, setFormData] = useState({
        title: '',
        url: '',
        credibility_score: 0.5,
        source_type: 'website'
    });
    const [actionLoading, setActionLoading] = useState(false);

    useEffect(() => {
        const token = localStorage.getItem('adminToken');
        if (!token) {
            navigate('/admin/login');
            return;
        }
        fetchSources(token);
    }, [navigate, page, searchTerm]);

    const fetchSources = async (token) => {
        setLoading(true);
        try {
            const url = `${BASE_URL}/admin/sources/?page=${page}&per_page=20&search=${searchTerm}`;
            
            const response = await fetch(url, {
                headers: {
                    'Authorization': `Bearer ${token}`,
                    'Content-Type': 'application/json'
                }
            });

            if (response.status === 401) {
                localStorage.clear();
                navigate('/admin/login');
                return;
            }

            if (!response.ok) {
                throw new Error('Failed to fetch sources');
            }

            const data = await response.json();
            setSources(data.sources || []);
            setPagination(data.pagination || {});
            setLoading(false);
        } catch (err) {
            console.error('Error fetching sources:', err);
            setError('Failed to load sources');
            setLoading(false);
        }
    };

    const handleCreateSource = async (e) => {
        e.preventDefault();
        const token = localStorage.getItem('adminToken');
        setActionLoading(true);

        try {
            const response = await fetch(`${BASE_URL}/admin/sources/`, {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${token}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(formData)
            });

            if (!response.ok) {
                const data = await response.json();
                throw new Error(data.error || 'Failed to create source');
            }

            const data = await response.json();
            alert(data.message);
            
            setShowModal(false);
            resetForm();
            fetchSources(token);
        } catch (err) {
            console.error('Error creating source:', err);
            alert(err.message);
        } finally {
            setActionLoading(false);
        }
    };

    const handleUpdateSource = async (e) => {
        e.preventDefault();
        const token = localStorage.getItem('adminToken');
        setActionLoading(true);

        try {
            const response = await fetch(`${BASE_URL}/admin/sources/${editingSource.id}/`, {
                method: 'PUT',
                headers: {
                    'Authorization': `Bearer ${token}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(formData)
            });

            if (!response.ok) {
                const data = await response.json();
                throw new Error(data.error || 'Failed to update source');
            }

            const data = await response.json();
            alert(data.message);
            
            setShowModal(false);
            setEditingSource(null);
            resetForm();
            fetchSources(token);
        } catch (err) {
            console.error('Error updating source:', err);
            alert(err.message);
        } finally {
            setActionLoading(false);
        }
    };

    const handleDeleteSource = async (sourceId, title) => {
        if (!confirm(`Are you sure you want to delete "${title}"?`)) return;

        const token = localStorage.getItem('adminToken');
        
        try {
            const response = await fetch(`${BASE_URL}/admin/sources/${sourceId}/`, {
                method: 'DELETE',
                headers: {
                    'Authorization': `Bearer ${token}`,
                    'Content-Type': 'application/json'
                }
            });

            if (!response.ok) throw new Error('Failed to delete source');

            const data = await response.json();
            alert(data.message);
            fetchSources(token);
        } catch (err) {
            console.error('Error deleting source:', err);
            alert('Failed to delete source');
        }
    };

    const openCreateModal = () => {
        setEditingSource(null);
        resetForm();
        setShowModal(true);
    };

    const openEditModal = (source) => {
        setEditingSource(source);
        setFormData({
            title: source.title,
            url: source.url,
            credibility_score: source.credibility_score,
            source_type: source.source_type
        });
        setShowModal(true);
    };

    const resetForm = () => {
        setFormData({
            title: '',
            url: '',
            credibility_score: 0.5,
            source_type: 'website'
        });
    };

    const handleLogout = () => {
        localStorage.clear();
        navigate('/admin/login');
    };

    if (loading) {
        return (
            <div className="flex items-center justify-center min-h-screen">
                <div className="text-center">
                    <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600 mx-auto mb-4"></div>
                    <p className="text-gray-600">Loading sources...</p>
                </div>
            </div>
        );
    }

    return (
        <div className="min-h-screen bg-gray-50">
            {/* Header */}
            <header className="bg-gradient-to-r from-blue-600 to-cyan-600 shadow-lg">
                <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
                    <div className="flex justify-between items-center">
                        <div>
                            <div className="flex items-center gap-3 mb-2">
                                <BookOpen className="w-8 h-8 text-white" />
                                <h1 className="text-2xl sm:text-3xl font-bold text-white">Sources Management</h1>
                            </div>
                            <p className="text-sm text-blue-100">Manage verification sources</p>
                        </div>
                        <div className="flex gap-3">
                            <button
                                onClick={() => navigate('/admin/dashboard')}
                                className="inline-flex items-center gap-2 px-4 py-2 bg-white/20 hover:bg-white/30 text-white rounded-lg transition backdrop-blur-sm border border-white/20"
                            >
                                <ArrowLeft className="w-4 h-4" />
                                Dashboard
                            </button>
                            <button
                                onClick={handleLogout}
                                className="inline-flex items-center gap-2 px-4 py-2 bg-white/20 hover:bg-white/30 text-white rounded-lg transition backdrop-blur-sm border border-white/20"
                            >
                                <LogOut className="w-4 h-4" />
                                Logout
                            </button>
                        </div>
                    </div>
                </div>
            </header>

            {/* Main Content */}
            <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
                {error && (
                    <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg mb-6 flex items-start gap-2">
                        <AlertCircle className="w-5 h-5 flex-shrink-0 mt-0.5" />
                        <span>{error}</span>
                    </div>
                )}

                {/* Search and Add */}
                <div className="bg-white rounded-2xl shadow-xl border border-slate-100 p-6 mb-6">
                    <div className="flex flex-col md:flex-row gap-4 items-end">
                        <div className="flex-1">
                            <label className="block text-sm font-semibold text-gray-700 mb-2">
                                Search Sources
                            </label>
                            <div className="relative">
                                <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                                    <Search className="w-5 h-5 text-gray-400" />
                                </div>
                                <input
                                    type="text"
                                    placeholder="Search by title or URL..."
                                    value={searchTerm}
                                    onChange={(e) => {
                                    setSearchTerm(e.target.value);
                                    setPage(1);
                                }}
                                    className="w-full pl-10 pr-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                                />
                            </div>
                        </div>
                        <button
                            onClick={openCreateModal}
                            className="inline-flex items-center gap-2 px-6 py-2 bg-gradient-to-r from-blue-600 to-cyan-600 hover:from-blue-700 hover:to-cyan-700 text-white font-semibold rounded-lg transition shadow-lg hover:shadow-xl whitespace-nowrap"
                        >
                            <Plus className="w-5 h-5" />
                            Add Source
                        </button>
                    </div>

                    {pagination.total !== undefined && (
                        <div className="mt-4 text-sm text-gray-600">
                            Showing {sources.length} of {pagination.total} sources
                        </div>
                    )}
                </div>

                {/* Sources Table */}
                <div className="bg-white rounded-lg shadow overflow-hidden">
                    <div className="w-full">
                        <table className="w-full divide-y divide-gray-200 text-xs sm:text-sm">
                            <thead className="bg-gray-50">
                                <tr>
                                    <th className="px-3 py-2 sm:px-4 md:px-6 text-left text-[10px] sm:text-xs font-medium text-gray-500 uppercase tracking-wider">
                                        ID
                                    </th>
                                    <th className="px-3 py-2 sm:px-4 md:px-6 text-left text-[10px] sm:text-xs font-medium text-gray-500 uppercase tracking-wider">
                                        Title
                                    </th>
                                    <th className="px-3 py-2 sm:px-4 md:px-6 text-left text-[10px] sm:text-xs font-medium text-gray-500 uppercase tracking-wider">
                                        URL
                                    </th>
                                    <th className="px-3 py-2 sm:px-4 md:px-6 text-left text-[10px] sm:text-xs font-medium text-gray-500 uppercase tracking-wider">
                                        Type
                                    </th>
                                    <th className="px-3 py-2 sm:px-4 md:px-6 text-left text-[10px] sm:text-xs font-medium text-gray-500 uppercase tracking-wider">
                                        Credibility
                                    </th>
                                    <th className="px-3 py-2 sm:px-4 md:px-6 text-left text-[10px] sm:text-xs font-medium text-gray-500 uppercase tracking-wider">
                                        Actions
                                    </th>
                                </tr>
                            </thead>
                            <tbody className="bg-white divide-y divide-gray-200">
                                {sources.length === 0 ? (
                                    <tr>
                                        <td colSpan="6" className="px-3 sm:px-4 md:px-6 py-6 text-center text-gray-500 text-xs sm:text-sm">
                                            No sources found
                                        </td>
                                    </tr>
                                ) : (
                                    sources.map((source) => (
                                        <tr key={source.id} className="hover:bg-gray-50">
                                            <td className="px-3 py-2 sm:px-4 md:px-6 text-[11px] sm:text-sm text-gray-900">
                                                #{source.id}
                                            </td>
                                            <td className="px-3 py-2 sm:px-4 md:px-6 text-[11px] sm:text-sm text-gray-900">
                                                <div className="max-w-[140px] sm:max-w-xs break-words">
                                                    {source.title}
                                                </div>
                                            </td>
                                            <td className="px-3 py-2 sm:px-4 md:px-6 text-[11px] sm:text-sm text-blue-600">
                                                <a
                                                    href={source.url}
                                                    target="_blank"
                                                    rel="noopener noreferrer"
                                                    className="hover:underline"
                                                >
                                                    <div className="max-w-[160px] sm:max-w-xs break-words">
                                                        {source.url}
                                                    </div>
                                                </a>
                                            </td>
                                            <td className="px-3 py-2 sm:px-4 md:px-6 text-[11px] sm:text-sm text-gray-900">
                                                <span className="px-2 py-1 bg-gray-100 rounded text-[10px] sm:text-xs">
                                                    {source.source_type}
                                                </span>
                                            </td>
                                            <td className="px-3 py-2 sm:px-4 md:px-6 text-[11px] sm:text-sm">
                                                <div className="flex items-center">
                                                    <div className="w-12 sm:w-16 bg-gray-200 rounded-full h-2 mr-2">
                                                        <div
                                                            className="bg-green-500 h-2 rounded-full"
                                                            style={{ width: `${source.credibility_score * 100}%` }}
                                                        ></div>
                                                    </div>
                                                    <span className="text-gray-700">
                                                        {(source.credibility_score * 100).toFixed(0)}%
                                                    </span>
                                                </div>
                                            </td>
                                            <td className="px-3 py-2 sm:px-4 md:px-6 text-[11px] sm:text-sm space-x-2">
                                                <button
                                                    onClick={() => openEditModal(source)}
                                                    className="text-blue-600 hover:text-blue-900 font-medium"
                                                >
                                                    Edit
                                                </button>
                                                <button
                                                    onClick={() => handleDeleteSource(source.id, source.title)}
                                                    className="text-red-600 hover:text-red-900 font-medium"
                                                >
                                                    Delete
                                                </button>
                                            </td>
                                        </tr>
                                    ))
                                )}
                            </tbody>
                        </table>
                    </div>

                    {/* Pagination */}
                    {pagination.total_pages > 1 && (
                        <div className="bg-gray-50 px-6 py-3 flex items-center justify-between border-t">
                            <button
                                onClick={() => setPage(p => Math.max(1, p - 1))}
                                disabled={page === 1}
                                className="px-4 py-2 bg-white border rounded-lg disabled:opacity-50 disabled:cursor-not-allowed hover:bg-gray-50"
                            >
                                Previous
                            </button>
                            <span className="text-sm text-gray-700">
                                Page {page} of {pagination.total_pages}
                            </span>
                            <button
                                onClick={() => setPage(p => Math.min(pagination.total_pages, p + 1))}
                                disabled={page === pagination.total_pages}
                                className="px-4 py-2 bg-white border rounded-lg disabled:opacity-50 disabled:cursor-not-allowed hover:bg-gray-50"
                            >
                                Next
                            </button>
                        </div>
                    )}
                </div>
            </main>

            {/* Modal */}
            {showModal && (
                <SourceModal
                    isEdit={!!editingSource}
                    formData={formData}
                    setFormData={setFormData}
                    onSubmit={editingSource ? handleUpdateSource : handleCreateSource}
                    onClose={() => {
                        setShowModal(false);
                        setEditingSource(null);
                        resetForm();
                    }}
                    loading={actionLoading}
                />
            )}
        </div>
    );
};

// Source Modal Component
const SourceModal = ({ isEdit, formData, setFormData, onSubmit, onClose, loading }) => {
    return (
        <div className="fixed inset-0 backdrop-blur-sm flex items-center justify-center p-4 z-50">
            <div className="bg-white rounded-2xl shadow-2xl max-w-md w-full border-2 border-blue-100">
                <div className="p-6">
                    <div className="flex justify-between items-center mb-4">
                        <h2 className="text-2xl font-bold text-gray-900">
                            {isEdit ? 'Edit Source' : 'Add New Source'}
                        </h2>
                        <button
                            onClick={onClose}
                            className="text-gray-500 hover:text-gray-700 text-2xl"
                        >
                            ✕
                        </button>
                    </div>

                    <form onSubmit={onSubmit} className="space-y-4">
                        <div>
                            <label className="block text-sm font-medium text-gray-700 mb-1">
                                Title *
                            </label>
                            <input
                                type="text"
                                required
                                value={formData.title}
                                onChange={(e) => setFormData({ ...formData, title: e.target.value })}
                                className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                                placeholder="e.g., WHO Official Website"
                            />
                        </div>

                        <div>
                            <label className="block text-sm font-medium text-gray-700 mb-1">
                                URL *
                            </label>
                            <input
                                type="url"
                                required
                                value={formData.url}
                                onChange={(e) => setFormData({ ...formData, url: e.target.value })}
                                className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                                placeholder="https://..."
                            />
                        </div>

                        <div>
                            <label className="block text-sm font-medium text-gray-700 mb-1">
                                Source Type
                            </label>
                            <select
                                value={formData.source_type}
                                onChange={(e) => setFormData({ ...formData, source_type: e.target.value })}
                                className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                            >
                                <option value="website">Website</option>
                                <option value="journal">Journal</option>
                                <option value="news">News</option>
                                <option value="government">Government</option>
                                <option value="organization">Organization</option>
                                <option value="other">Other</option>
                            </select>
                        </div>

                        <div>
                            <label className="block text-sm font-medium text-gray-700 mb-1">
                                Credibility Score: {(formData.credibility_score * 100).toFixed(0)}%
                            </label>
                            <input
                                type="range"
                                min="0"
                                max="1"
                                step="0.01"
                                value={formData.credibility_score}
                                onChange={(e) => setFormData({ ...formData, credibility_score: parseFloat(e.target.value) })}
                                className="w-full"
                            />
                            <div className="flex justify-between text-xs text-gray-500 mt-1">
                                <span>Low (0%)</span>
                                <span>High (100%)</span>
                            </div>
                        </div>

                        <div className="flex gap-3 pt-4">
                            <button
                                type="button"
                                onClick={onClose}
                                className="flex-1 px-4 py-2 bg-gray-200 hover:bg-gray-300 text-gray-800 rounded-lg transition"
                            >
                                Cancel
                            </button>
                            <button
                                type="submit"
                                disabled={loading}
                                className="flex-1 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg transition disabled:bg-gray-400"
                            >
                                {loading ? 'Saving...' : (isEdit ? 'Update' : 'Create')}
                            </button>
                        </div>
                    </form>
                </div>
            </div>
        </div>
    );
};

export default AdminSources;
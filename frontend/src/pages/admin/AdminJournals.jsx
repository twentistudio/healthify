import { AlertCircle, ArrowLeft, BookOpen, Edit, ExternalLink, LogOut, Plus, RefreshCw, Search, Trash2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';

const BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api';

const AdminJournals = () => {
    const navigate = useNavigate();
    const [journals, setJournals] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [searchTerm, setSearchTerm] = useState('');
    const [sourceFilter, setSourceFilter] = useState('');
    const [page, setPage] = useState(1);
    const [pagination, setPagination] = useState({});
    const [showModal, setShowModal] = useState(false);
    const [editingJournal, setEditingJournal] = useState(null);
    const [actionLoading, setActionLoading] = useState(false);
    const [embedLoading, setEmbedLoading] = useState(false);

    const [formData, setFormData] = useState({
        title: '',
        abstract: '',
        doi: '',
        url: '',
        authors: '',
        journal_name: '',
        publisher: '',
        source_portal: 'sinta',
        keywords: '',
        published_date: ''
    });

    const sourceOptions = [
        { value: 'sinta', label: 'SINTA' },
        { value: 'garuda', label: 'Garuda' },
        { value: 'doaj', label: 'DOAJ' },
        { value: 'google_scholar', label: 'Google Scholar' },
        { value: 'other', label: 'Other' }
    ];

    useEffect(() => {
        const token = localStorage.getItem('adminToken');
        if (!token) {
            navigate('/admin/login');
            return;
        }
        fetchJournals(token);
    }, [navigate, page, searchTerm, sourceFilter]);

    const fetchJournals = async (token) => {
        setLoading(true);
        try {
            let url = `${BASE_URL}/admin/journals/?page=${page}&per_page=20`;
            if (searchTerm) url += `&search=${encodeURIComponent(searchTerm)}`;
            if (sourceFilter) url += `&source=${sourceFilter}`;

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
                throw new Error('Failed to fetch journals');
            }

            const data = await response.json();
            setJournals(data.journals || []);
            setPagination(data.pagination || {});
            setLoading(false);
        } catch (err) {
            console.error('Error fetching journals:', err);
            setError('Failed to load journals');
            setLoading(false);
        }
    };

    const handleCreateJournal = async (e) => {
        e.preventDefault();
        const token = localStorage.getItem('adminToken');
        setActionLoading(true);

        try {
            const response = await fetch(`${BASE_URL}/admin/journals/`, {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${token}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(formData)
            });

            if (!response.ok) {
                const data = await response.json();
                throw new Error(data.error || 'Failed to create journal');
            }

            const data = await response.json();
            alert(data.message);

            setShowModal(false);
            resetForm();
            fetchJournals(token);
        } catch (err) {
            console.error('Error creating journal:', err);
            alert(err.message);
        } finally {
            setActionLoading(false);
        }
    };

    const handleUpdateJournal = async (e) => {
        e.preventDefault();
        const token = localStorage.getItem('adminToken');
        setActionLoading(true);

        try {
            const response = await fetch(`${BASE_URL}/admin/journals/${editingJournal.id}/`, {
                method: 'PUT',
                headers: {
                    'Authorization': `Bearer ${token}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(formData)
            });

            if (!response.ok) {
                const data = await response.json();
                throw new Error(data.error || 'Failed to update journal');
            }

            const data = await response.json();
            alert(data.message);

            setShowModal(false);
            setEditingJournal(null);
            resetForm();
            fetchJournals(token);
        } catch (err) {
            console.error('Error updating journal:', err);
            alert(err.message);
        } finally {
            setActionLoading(false);
        }
    };

    const handleDeleteJournal = async (journalId, title) => {
        if (!confirm(`Apakah Anda yakin ingin menghapus jurnal "${title}"?`)) return;

        const token = localStorage.getItem('adminToken');

        try {
            const response = await fetch(`${BASE_URL}/admin/journals/${journalId}/`, {
                method: 'DELETE',
                headers: {
                    'Authorization': `Bearer ${token}`,
                    'Content-Type': 'application/json'
                }
            });

            if (!response.ok) throw new Error('Failed to delete journal');

            const data = await response.json();
            alert(data.message);
            fetchJournals(token);
        } catch (err) {
            console.error('Error deleting journal:', err);
            alert('Failed to delete journal');
        }
    };

    const handleEmbedAll = async () => {
        const token = localStorage.getItem('adminToken');
        setEmbedLoading(true);

        try {
            const response = await fetch(`${BASE_URL}/admin/journals/embed/`, {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${token}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({})
            });

            if (!response.ok) throw new Error('Failed to embed journals');

            const data = await response.json();
            alert(data.message);
            fetchJournals(token);
        } catch (err) {
            console.error('Error embedding journals:', err);
            alert('Failed to embed journals');
        } finally {
            setEmbedLoading(false);
        }
    };

    const openCreateModal = () => {
        setEditingJournal(null);
        resetForm();
        setShowModal(true);
    };

    const openEditModal = (journal) => {
        setEditingJournal(journal);
        setFormData({
            title: journal.title || '',
            abstract: journal.abstract || '',
            doi: journal.doi || '',
            url: journal.url || '',
            authors: journal.authors || '',
            journal_name: journal.journal_name || '',
            publisher: journal.publisher || '',
            source_portal: journal.source_portal || 'other',
            keywords: journal.keywords || '',
            published_date: journal.published_date || ''
        });
        setShowModal(true);
    };

    const resetForm = () => {
        setFormData({
            title: '',
            abstract: '',
            doi: '',
            url: '',
            authors: '',
            journal_name: '',
            publisher: '',
            source_portal: 'sinta',
            keywords: '',
            published_date: ''
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
                    <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-green-600 mx-auto mb-4"></div>
                    <p className="text-gray-600">Loading journals...</p>
                </div>
            </div>
        );
    }

    return (
        <div className="min-h-screen bg-gray-50">
            {/* Header */}
            <header className="bg-gradient-to-r from-green-600 to-teal-600 shadow-lg">
                <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
                    <div className="flex justify-between items-center">
                        <div>
                            <div className="flex items-center gap-3 mb-2">
                                <BookOpen className="w-8 h-8 text-white" />
                                <h1 className="text-2xl sm:text-3xl font-bold text-white">Manajemen Jurnal Indonesia</h1>
                            </div>
                            <p className="text-sm text-green-100">Import jurnal dari SINTA, Garuda, dan sumber Indonesia lainnya untuk RAG</p>
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

                {/* Search, Filter and Actions */}
                <div className="bg-white rounded-2xl shadow-xl border border-slate-100 p-6 mb-6">
                    <div className="flex flex-col lg:flex-row gap-4 items-end">
                        <div className="flex-1">
                            <label className="block text-sm font-semibold text-gray-700 mb-2">
                                Cari Jurnal
                            </label>
                            <div className="relative">
                                <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                                    <Search className="w-5 h-5 text-gray-400" />
                                </div>
                                <input
                                    type="text"
                                    placeholder="Cari berdasarkan judul, abstrak, atau kata kunci..."
                                    value={searchTerm}
                                    onChange={(e) => {
                                        setSearchTerm(e.target.value);
                                        setPage(1);
                                    }}
                                    className="w-full pl-10 pr-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-green-500 focus:border-transparent"
                                />
                            </div>
                        </div>

                        <div className="w-full lg:w-48">
                            <label className="block text-sm font-semibold text-gray-700 mb-2">
                                Sumber Portal
                            </label>
                            <select
                                value={sourceFilter}
                                onChange={(e) => {
                                    setSourceFilter(e.target.value);
                                    setPage(1);
                                }}
                                className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-green-500 focus:border-transparent"
                            >
                                <option value="">Semua Sumber</option>
                                {sourceOptions.map(opt => (
                                    <option key={opt.value} value={opt.value}>{opt.label}</option>
                                ))}
                            </select>
                        </div>

                        <button
                            onClick={handleEmbedAll}
                            disabled={embedLoading}
                            className="inline-flex items-center gap-2 px-4 py-2 bg-purple-600 hover:bg-purple-700 text-white font-semibold rounded-lg transition shadow-lg hover:shadow-xl whitespace-nowrap disabled:opacity-50"
                        >
                            <RefreshCw className={`w-5 h-5 ${embedLoading ? 'animate-spin' : ''}`} />
                            {embedLoading ? 'Embedding...' : 'Embed Semua'}
                        </button>

                        <button
                            onClick={openCreateModal}
                            className="inline-flex items-center gap-2 px-6 py-2 bg-gradient-to-r from-green-600 to-teal-600 hover:from-green-700 hover:to-teal-700 text-white font-semibold rounded-lg transition shadow-lg hover:shadow-xl whitespace-nowrap"
                        >
                            <Plus className="w-5 h-5" />
                            Tambah Jurnal
                        </button>
                    </div>

                    {pagination.total !== undefined && (
                        <div className="mt-4 text-sm text-gray-600">
                            Menampilkan {journals.length} dari {pagination.total} jurnal
                        </div>
                    )}
                </div>

                {/* Journals Table */}
                <div className="bg-white rounded-lg shadow overflow-hidden">
                    <div className="overflow-x-auto">
                        <table className="w-full divide-y divide-gray-200 text-xs sm:text-sm">
                            <thead className="bg-gray-50">
                                <tr>
                                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">ID</th>
                                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Judul</th>
                                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">DOI</th>
                                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Sumber</th>
                                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Status Embed</th>
                                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Aksi</th>
                                </tr>
                            </thead>
                            <tbody className="bg-white divide-y divide-gray-200">
                                {journals.length === 0 ? (
                                    <tr>
                                        <td colSpan="6" className="px-4 py-8 text-center text-gray-500">
                                            Belum ada jurnal. Klik "Tambah Jurnal" untuk menambahkan.
                                        </td>
                                    </tr>
                                ) : (
                                    journals.map((journal) => (
                                        <tr key={journal.id} className="hover:bg-gray-50">
                                            <td className="px-4 py-3 text-gray-900">#{journal.id}</td>
                                            <td className="px-4 py-3 text-gray-900">
                                                <div className="max-w-xs truncate" title={journal.title}>
                                                    {journal.title}
                                                </div>
                                                {journal.journal_name && (
                                                    <div className="text-xs text-gray-500">{journal.journal_name}</div>
                                                )}
                                            </td>
                                            <td className="px-4 py-3 text-blue-600">
                                                {journal.doi ? (
                                                    <a
                                                        href={`https://doi.org/${journal.doi}`}
                                                        target="_blank"
                                                        rel="noopener noreferrer"
                                                        className="hover:underline flex items-center gap-1"
                                                    >
                                                        <span className="truncate max-w-[120px]">{journal.doi}</span>
                                                        <ExternalLink className="w-3 h-3" />
                                                    </a>
                                                ) : (
                                                    <span className="text-gray-400">-</span>
                                                )}
                                            </td>
                                            <td className="px-4 py-3">
                                                <span className={`px-2 py-1 rounded text-xs font-medium ${
                                                    journal.source_portal === 'sinta' ? 'bg-blue-100 text-blue-800' :
                                                    journal.source_portal === 'garuda' ? 'bg-green-100 text-green-800' :
                                                    journal.source_portal === 'doaj' ? 'bg-yellow-100 text-yellow-800' :
                                                    'bg-gray-100 text-gray-800'
                                                }`}>
                                                    {journal.source_portal?.toUpperCase() || 'OTHER'}
                                                </span>
                                            </td>
                                            <td className="px-4 py-3">
                                                {journal.is_embedded ? (
                                                    <span className="px-2 py-1 bg-green-100 text-green-800 rounded text-xs font-medium">
                                                        ✓ Embedded
                                                    </span>
                                                ) : (
                                                    <span className="px-2 py-1 bg-yellow-100 text-yellow-800 rounded text-xs font-medium">
                                                        Pending
                                                    </span>
                                                )}
                                            </td>
                                            <td className="px-4 py-3 space-x-2">
                                                <button
                                                    onClick={() => openEditModal(journal)}
                                                    className="text-blue-600 hover:text-blue-900 font-medium inline-flex items-center gap-1"
                                                >
                                                    <Edit className="w-4 h-4" />
                                                    Edit
                                                </button>
                                                <button
                                                    onClick={() => handleDeleteJournal(journal.id, journal.title)}
                                                    className="text-red-600 hover:text-red-900 font-medium inline-flex items-center gap-1"
                                                >
                                                    <Trash2 className="w-4 h-4" />
                                                    Hapus
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
                                Sebelumnya
                            </button>
                            <span className="text-sm text-gray-700">
                                Halaman {page} dari {pagination.total_pages}
                            </span>
                            <button
                                onClick={() => setPage(p => Math.min(pagination.total_pages, p + 1))}
                                disabled={page === pagination.total_pages}
                                className="px-4 py-2 bg-white border rounded-lg disabled:opacity-50 disabled:cursor-not-allowed hover:bg-gray-50"
                            >
                                Selanjutnya
                            </button>
                        </div>
                    )}
                </div>
            </main>

            {/* Modal */}
            {showModal && (
                <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
                    <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl max-h-[90vh] overflow-y-auto">
                        <div className="p-6 border-b">
                            <h2 className="text-xl font-bold text-gray-900">
                                {editingJournal ? 'Edit Jurnal' : 'Tambah Jurnal Baru'}
                            </h2>
                            <p className="text-sm text-gray-500 mt-1">
                                {editingJournal ? 'Perbarui informasi jurnal' : 'Input data jurnal dari sumber Indonesia'}
                            </p>
                        </div>

                        <form onSubmit={editingJournal ? handleUpdateJournal : handleCreateJournal} className="p-6 space-y-4">
                            <div>
                                <label className="block text-sm font-medium text-gray-700 mb-1">
                                    Judul <span className="text-red-500">*</span>
                                </label>
                                <input
                                    type="text"
                                    required
                                    value={formData.title}
                                    onChange={(e) => setFormData({ ...formData, title: e.target.value })}
                                    className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-green-500 focus:border-transparent"
                                    placeholder="Judul lengkap jurnal"
                                />
                            </div>

                            <div>
                                <label className="block text-sm font-medium text-gray-700 mb-1">
                                    Abstrak <span className="text-red-500">*</span>
                                </label>
                                <textarea
                                    required
                                    rows={5}
                                    value={formData.abstract}
                                    onChange={(e) => setFormData({ ...formData, abstract: e.target.value })}
                                    className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-green-500 focus:border-transparent"
                                    placeholder="Abstrak jurnal dalam Bahasa Indonesia atau Inggris"
                                />
                            </div>

                            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                <div>
                                    <label className="block text-sm font-medium text-gray-700 mb-1">DOI</label>
                                    <input
                                        type="text"
                                        value={formData.doi}
                                        onChange={(e) => setFormData({ ...formData, doi: e.target.value })}
                                        className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-green-500 focus:border-transparent"
                                        placeholder="10.xxxx/xxxxx"
                                    />
                                </div>

                                <div>
                                    <label className="block text-sm font-medium text-gray-700 mb-1">URL</label>
                                    <input
                                        type="url"
                                        value={formData.url}
                                        onChange={(e) => setFormData({ ...formData, url: e.target.value })}
                                        className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-green-500 focus:border-transparent"
                                        placeholder="https://..."
                                    />
                                </div>
                            </div>

                            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                <div>
                                    <label className="block text-sm font-medium text-gray-700 mb-1">Penulis</label>
                                    <input
                                        type="text"
                                        value={formData.authors}
                                        onChange={(e) => setFormData({ ...formData, authors: e.target.value })}
                                        className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-green-500 focus:border-transparent"
                                        placeholder="Nama penulis, dipisah koma"
                                    />
                                </div>

                                <div>
                                    <label className="block text-sm font-medium text-gray-700 mb-1">Nama Jurnal</label>
                                    <input
                                        type="text"
                                        value={formData.journal_name}
                                        onChange={(e) => setFormData({ ...formData, journal_name: e.target.value })}
                                        className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-green-500 focus:border-transparent"
                                        placeholder="Nama jurnal/publikasi"
                                    />
                                </div>
                            </div>

                            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                                <div>
                                    <label className="block text-sm font-medium text-gray-700 mb-1">Sumber Portal</label>
                                    <select
                                        value={formData.source_portal}
                                        onChange={(e) => setFormData({ ...formData, source_portal: e.target.value })}
                                        className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-green-500 focus:border-transparent"
                                    >
                                        {sourceOptions.map(opt => (
                                            <option key={opt.value} value={opt.value}>{opt.label}</option>
                                        ))}
                                    </select>
                                </div>

                                <div>
                                    <label className="block text-sm font-medium text-gray-700 mb-1">Publisher</label>
                                    <input
                                        type="text"
                                        value={formData.publisher}
                                        onChange={(e) => setFormData({ ...formData, publisher: e.target.value })}
                                        className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-green-500 focus:border-transparent"
                                        placeholder="Nama penerbit"
                                    />
                                </div>

                                <div>
                                    <label className="block text-sm font-medium text-gray-700 mb-1">Tanggal Publikasi</label>
                                    <input
                                        type="date"
                                        value={formData.published_date}
                                        onChange={(e) => setFormData({ ...formData, published_date: e.target.value })}
                                        className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-green-500 focus:border-transparent"
                                    />
                                </div>
                            </div>

                            <div>
                                <label className="block text-sm font-medium text-gray-700 mb-1">Kata Kunci</label>
                                <input
                                    type="text"
                                    value={formData.keywords}
                                    onChange={(e) => setFormData({ ...formData, keywords: e.target.value })}
                                    className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-green-500 focus:border-transparent"
                                    placeholder="kata kunci, dipisah koma"
                                />
                            </div>

                            <div className="flex justify-end gap-3 pt-4 border-t">
                                <button
                                    type="button"
                                    onClick={() => {
                                        setShowModal(false);
                                        setEditingJournal(null);
                                        resetForm();
                                    }}
                                    className="px-6 py-2 border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50 transition"
                                >
                                    Batal
                                </button>
                                <button
                                    type="submit"
                                    disabled={actionLoading}
                                    className="px-6 py-2 bg-gradient-to-r from-green-600 to-teal-600 text-white rounded-lg hover:from-green-700 hover:to-teal-700 transition disabled:opacity-50"
                                >
                                    {actionLoading ? 'Menyimpan...' : (editingJournal ? 'Simpan Perubahan' : 'Tambah Jurnal')}
                                </button>
                            </div>
                        </form>
                    </div>
                </div>
            )}
        </div>
    );
};

export default AdminJournals;

import { AlertTriangle, ArrowLeft, Calendar, CheckCircle2, Eye, FileText, Filter as FilterIcon, HelpCircle, LogOut, Search, TrendingUp, XCircle } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';

const BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api';

const AdminClaims = () => {
    const navigate = useNavigate();
    const [claims, setClaims] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [searchTerm, setSearchTerm] = useState('');
    const [filterLabel, setFilterLabel] = useState('all');

    useEffect(() => {
        const token = localStorage.getItem('adminToken');
        if (!token) {
            navigate('/admin/login');
            return;
        }
        fetchClaims(token);
    }, [navigate]);

    const fetchClaims = async (token) => {
    try {
        console.log('[FETCH_CLAIMS] Starting fetch...'); 
        
        const response = await fetch(`${BASE_URL}/claims/`, {
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            }
        });

        console.log('[FETCH_CLAIMS] Response status:', response.status); 

        if (response.status === 401) {
            localStorage.clear();
            navigate('/admin/login');
            return;
        }

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            console.error('[FETCH_CLAIMS] Error response:', errorData); 
            throw new Error('Failed to fetch claims');
        }

        const data = await response.json();
        console.log('[FETCH_CLAIMS] Response data:', data); 
        console.log('[FETCH_CLAIMS] Claims count:', data.claims?.length);         
        setClaims(data.claims || []);
        setLoading(false);
    } catch (err) {
        console.error('Error fetching claims:', err);
        setError('Failed to load claims');
        setLoading(false);
    }
};

    const handleLogout = () => {
        localStorage.clear();
        navigate('/admin/login');
    };

    const filteredClaims = claims.filter(claim => {
        const matchesSearch = claim.text.toLowerCase().includes(searchTerm.toLowerCase());
        
        // Map filter value to backend label format
        const labelMap = {
            'valid': 'valid',
            'hoax': 'hoax',
            'uncertain': 'uncertain',
            'unverified': 'unverified'
        };
        const mappedLabel = labelMap[filterLabel] || filterLabel;
        const matchesFilter = filterLabel === 'all' || claim.label.toLowerCase() === mappedLabel;
        
        return matchesSearch && matchesFilter;
    });

    const getLabelColor = (label) => {
        const colors = {
            'TRUE': 'bg-green-100 text-green-800',
            'FALSE': 'bg-red-100 text-red-800',
            'MIXTURE': 'bg-yellow-100 text-yellow-800',
            'UNVERIFIED': 'bg-gray-100 text-gray-800'
        };
        return colors[label] || 'bg-gray-100 text-gray-800';
    };

    if (loading) {
        return (
            <div className="flex items-center justify-center min-h-screen">
                <div className="text-center">
                    <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600 mx-auto mb-4"></div>
                    <p className="text-gray-600">Loading claims...</p>
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
                                <FileText className="w-8 h-8 text-white" />
                                <h1 className="text-2xl sm:text-3xl font-bold text-white">Claims Management</h1>
                            </div>
                            <p className="text-sm text-blue-100">Manage and review all claims</p>
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
                        <AlertTriangle className="w-5 h-5 flex-shrink-0 mt-0.5" />
                        <span>{error}</span>
                    </div>
                )}

                {/* Filters */}
                <div className="bg-white rounded-2xl shadow-xl border border-slate-100 p-6 mb-6">
                    <div className="flex items-center gap-2 mb-4">
                        <FilterIcon className="w-5 h-5 text-blue-600" />
                        <h2 className="text-lg font-bold text-gray-800">Search & Filter</h2>
                    </div>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        {/* Search */}
                        <div>
                            <label className="block text-sm font-semibold text-gray-700 mb-2">
                                Search Claims
                            </label>
                            <div className="relative">
                                <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                                    <Search className="w-5 h-5 text-gray-400" />
                                </div>
                                <input
                                    type="text"
                                    placeholder="Search by claim text..."
                                    value={searchTerm}
                                    onChange={(e) => setSearchTerm(e.target.value)}
                                    className="w-full pl-10 pr-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                                />
                            </div>
                        </div>

                        {/* Filter by Label */}
                        <div>
                            <label className="block text-sm font-semibold text-gray-700 mb-2">
                                Filter by Label
                            </label>
                            <select
                                value={filterLabel}
                                onChange={(e) => setFilterLabel(e.target.value)}
                                className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                            >
                                <option value="all">All Labels</option>
                                <option value="valid">Valid (Fakta)</option>
                                <option value="hoax">Hoax (Salah)</option>
                                <option value="uncertain">Uncertain (Tidak Pasti)</option>
                                <option value="unverified">Unverified (Belum Diverifikasi)</option>
                            </select>
                        </div>
                    </div>

                    <div className="mt-4 flex items-center gap-2 text-sm text-gray-600">
                        <TrendingUp className="w-4 h-4" />
                        <span>Showing <span className="font-semibold text-blue-600">{filteredClaims.length}</span> of <span className="font-semibold">{claims.length}</span> claims</span>
                    </div>
                </div>

                {/* Claims Cards */}
                <div className="space-y-4">
                    {filteredClaims.length === 0 ? (
                        <div className="bg-white rounded-2xl shadow-xl border border-slate-100 p-12 text-center">
                            <FileText className="w-16 h-16 text-gray-300 mx-auto mb-4" />
                            <p className="text-gray-500 text-lg font-medium">No claims found</p>
                            <p className="text-gray-400 text-sm mt-2">Try adjusting your search or filter criteria</p>
                        </div>
                    ) : (
                        filteredClaims.map((claim) => {
                            const labelConfig = {
                                'valid': { bg: 'bg-green-50', border: 'border-green-200', text: 'text-green-800', icon: <CheckCircle2 className="w-5 h-5" />, display: 'Valid' },
                                'hoax': { bg: 'bg-red-50', border: 'border-red-200', text: 'text-red-800', icon: <XCircle className="w-5 h-5" />, display: 'Hoax' },
                                'uncertain': { bg: 'bg-yellow-50', border: 'border-yellow-200', text: 'text-yellow-800', icon: <AlertTriangle className="w-5 h-5" />, display: 'Uncertain' },
                                'unverified': { bg: 'bg-gray-50', border: 'border-gray-200', text: 'text-gray-800', icon: <HelpCircle className="w-5 h-5" />, display: 'Unverified' }
                            };
                            const config = labelConfig[claim.label.toLowerCase()] || labelConfig['unverified'];
                            
                            return (
                                <div 
                                    key={claim.id}
                                    className="bg-white rounded-2xl shadow-lg border border-slate-100 p-6 hover:shadow-xl transition-shadow"
                                >
                                    <div className="flex flex-col lg:flex-row gap-6">
                                        {/* Left Side - Content */}
                                        <div className="flex-1 space-y-4">
                                            {/* Header */}
                                            <div className="flex items-start justify-between gap-4">
                                                <div className="flex items-center gap-3">
                                                    <div className={`${config.bg} ${config.border} border p-2 rounded-lg`}>
                                                        <div className={config.text}>
                                                            {config.icon}
                                                        </div>
                                                    </div>
                                                    <div>
                                                        <div className="flex items-center gap-2">
                                                            <span className="text-xs font-bold text-gray-500">Claim</span>
                                                            <span className="text-sm font-bold text-gray-900">#{claim.id}</span>
                                                        </div>
                                                        <span className={`inline-flex items-center gap-1 px-2 py-0.5 text-xs font-semibold rounded-full ${config.bg} ${config.text} ${config.border} border mt-1`}>
                                                            {config.display}
                                                        </span>
                                                    </div>
                                                </div>
                                                <div className="flex items-center gap-2 text-xs text-gray-500">
                                                    <Calendar className="w-4 h-4" />
                                                    {new Date(claim.created_at).toLocaleDateString('id-ID', { day: 'numeric', month: 'short', year: 'numeric' })}
                                                </div>
                                            </div>

                                            {/* Claim Text */}
                                            <div>
                                                <div className="flex items-center gap-2 mb-2">
                                                    <FileText className="w-4 h-4 text-blue-600" />
                                                    <label className="text-xs font-semibold text-gray-700">Claim Text</label>
                                                </div>
                                                <p className="text-sm text-gray-900 bg-gray-50 p-3 rounded-lg border border-gray-200 line-clamp-2">
                                                    {claim.text}
                                                </p>
                                            </div>

                                            {/* Confidence */}
                                            <div className="flex items-center gap-2">
                                                <TrendingUp className="w-4 h-4 text-gray-600" />
                                                <span className="text-xs font-semibold text-gray-700">Confidence:</span>
                                                <span className="text-sm font-bold text-blue-600">
                                                    {claim.confidence ? `${(claim.confidence * 100).toFixed(1)}%` : 'N/A'}
                                                </span>
                                            </div>
                                        </div>

                                        {/* Right Side - Actions */}
                                        <div className="lg:w-48 flex flex-col justify-center gap-3">
                                            <button
                                                onClick={() => navigate(`/admin/claims/${claim.id}`)}
                                                className="w-full inline-flex items-center justify-center gap-2 px-4 py-3 bg-gradient-to-r from-blue-600 to-cyan-600 hover:from-blue-700 hover:to-cyan-700 text-white font-semibold rounded-lg transition shadow-lg hover:shadow-xl"
                                            >
                                                <Eye className="w-4 h-4" />
                                                View Details
                                            </button>
                                        </div>
                                    </div>
                                </div>
                            );
                        })
                    )}
                </div>
            </main>
        </div>
    );
};

export default AdminClaims;
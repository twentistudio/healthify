import { useState, useEffect } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, ExternalLink, Calendar, Info } from 'react-feather';

const BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api';

const AdminClaimDetail = () => {
    const navigate = useNavigate();
    const { claimId } = useParams();
    const [claim, setClaim] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');

    useEffect(() => {
        const token = localStorage.getItem('adminToken');
        if (!token) {
            navigate('/admin/login');
            return;
        }
        fetchClaimDetail(token);
    }, [navigate, claimId]);

    const fetchClaimDetail = async (token) => {
        setLoading(true);
        try {
            const response = await fetch(`${BASE_URL}/claims/${claimId}/`, {
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
                throw new Error('Failed to fetch claim details');
            }

            const data = await response.json();
            setClaim(data);
            setLoading(false);
        } catch (err) {
            console.error('Error fetching claim details:', err);
            setError('Failed to load claim details');
            setLoading(false);
        }
    };

    const handleLogout = () => {
        localStorage.clear();
        navigate('/admin/login');
    };

    const getLabelColor = (label) => {
        const colors = {
            'TRUE': 'bg-green-100 text-green-800 border-green-300',
            'FALSE': 'bg-red-100 text-red-800 border-red-300',
            'MIXTURE': 'bg-yellow-100 text-yellow-800 border-yellow-300',
            'UNVERIFIED': 'bg-gray-100 text-gray-800 border-gray-300'
        };
        return colors[label] || 'bg-gray-100 text-gray-800 border-gray-300';
    };

    if (loading) {
        return (
            <div className="flex items-center justify-center min-h-screen">
                <div className="text-center">
                    <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600 mx-auto mb-4"></div>
                    <p className="text-gray-600">Loading claim details...</p>
                </div>
            </div>
        );
    }

    if (error || !claim) {
        return (
            <div className="min-h-screen bg-gray-50">
                <header className="bg-white shadow-sm">
                    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-4">
                        <div className="flex justify-between items-center">
                            <h1 className="text-2xl font-bold text-gray-900">Claim Details</h1>
                            <div className="flex gap-3">
                                <button
                                    onClick={() => navigate('/admin/claims')}
                                    className="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg transition"
                                >
                                    ← Back to Claims
                                </button>
                                <button
                                    onClick={handleLogout}
                                    className="px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg transition"
                                >
                                    Logout
                                </button>
                            </div>
                        </div>
                    </div>
                </header>
                <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
                    <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg">
                        {error || 'Claim not found'}
                    </div>
                </main>
            </div>
        );
    }

    return (
        <div className="min-h-screen bg-gray-50">
            {/* Header */}
            <header className="bg-white shadow-sm">
                <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-4">
                    <div className="flex justify-between items-center">
                        <div>
                            <h1 className="text-2xl font-bold text-gray-900">Claim Details</h1>
                            <p className="text-sm text-gray-600">Claim ID: #{claim.id}</p>
                        </div>
                        <div className="flex gap-3">
                            <button
                                onClick={() => navigate('/admin/claims')}
                                className="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg transition flex items-center gap-2"
                            >
                                <ArrowLeft className="w-4 h-4" />
                                Back to Claims
                            </button>
                            <button
                                onClick={handleLogout}
                                className="px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg transition"
                            >
                                Logout
                            </button>
                        </div>
                    </div>
                </div>
            </header>

            {/* Main Content */}
            <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
                <div className="space-y-6">
                    {/* Claim Text Card */}
                    <div className="bg-white rounded-lg shadow p-6">
                        <h2 className="text-xl font-semibold text-gray-900 mb-4">Claim Text</h2>
                        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
                            <p className="text-gray-800 text-lg">{claim.text}</p>
                        </div>
                    </div>

                    {/* Verification Result */}
                    {claim.verification_result && (
                        <div className="bg-white rounded-lg shadow p-6">
                            <h2 className="text-xl font-semibold text-gray-900 mb-4">Verification Result</h2>
                            
                            <div className="space-y-4">
                                {/* Label & Confidence */}
                                <div className="flex flex-wrap gap-3">
                                    <div className={`px-4 py-2 rounded-lg border ${getLabelColor(claim.verification_result.label)}`}>
                                        <div className="text-xs font-medium mb-1">Label</div>
                                        <div className="text-lg font-bold">{claim.verification_result.label}</div>
                                    </div>
                                    <div className="bg-gray-100 border border-gray-300 px-4 py-2 rounded-lg">
                                        <div className="text-xs font-medium text-gray-600 mb-1">Confidence</div>
                                        <div className="text-lg font-bold text-gray-800">
                                            {(claim.verification_result.confidence * 100).toFixed(1)}%
                                        </div>
                                    </div>
                                </div>

                                {/* Summary */}
                                {claim.verification_result.summary && (
                                    <div>
                                        <h3 className="text-sm font-semibold text-gray-700 mb-2">Summary</h3>
                                        <div className="bg-gray-50 rounded-lg p-4 border border-gray-200">
                                            <p className="text-gray-700 leading-relaxed">
                                                {claim.verification_result.summary}
                                            </p>
                                        </div>
                                    </div>
                                )}

                                {/* Timestamps */}
                                <div className="grid grid-cols-2 gap-4 pt-4 border-t">
                                    <div>
                                        <div className="text-xs text-gray-500 mb-1">Verified At</div>
                                        <div className="text-sm text-gray-800 flex items-center gap-2">
                                            <Calendar className="w-4 h-4" />
                                            {new Date(claim.verification_result.created_at).toLocaleString()}
                                        </div>
                                    </div>
                                    <div>
                                        <div className="text-xs text-gray-500 mb-1">Last Updated</div>
                                        <div className="text-sm text-gray-800 flex items-center gap-2">
                                            <Calendar className="w-4 h-4" />
                                            {new Date(claim.verification_result.updated_at).toLocaleString()}
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    )}

                    {/* Sources */}
                    {claim.sources && claim.sources.length > 0 && (
                        <div className="bg-white rounded-lg shadow p-6">
                            <h2 className="text-xl font-semibold text-gray-900 mb-4">
                                Sources ({claim.sources.length})
                            </h2>
                            <div className="space-y-4">
                                {claim.sources.map((sourceItem, index) => (
                                    <div 
                                        key={index}
                                        className="bg-gray-50 rounded-lg p-4 border border-gray-200 hover:border-blue-300 transition"
                                    >
                                        <div className="flex items-start justify-between gap-4">
                                            <div className="flex-1">
                                                <div className="flex items-center gap-2 mb-2">
                                                    <span className="bg-blue-100 text-blue-800 text-xs font-semibold px-2 py-1 rounded">
                                                        #{sourceItem.rank || index + 1}
                                                    </span>
                                                    <span className="bg-green-100 text-green-800 text-xs font-semibold px-2 py-1 rounded">
                                                        Relevance: {(sourceItem.relevance_score * 100).toFixed(1)}%
                                                    </span>
                                                </div>
                                                
                                                <h3 className="font-semibold text-gray-900 mb-2">
                                                    {sourceItem.source.title}
                                                </h3>
                                                
                                                {sourceItem.source.authors && (
                                                    <p className="text-sm text-gray-600 mb-2">
                                                        Authors: {sourceItem.source.authors}
                                                    </p>
                                                )}
                                                
                                                {sourceItem.source.publisher && (
                                                    <p className="text-sm text-gray-600 mb-2">
                                                        Publisher: {sourceItem.source.publisher}
                                                    </p>
                                                )}

                                                {sourceItem.excerpt && (
                                                    <div className="mt-3 bg-white rounded p-3 border border-gray-200">
                                                        <p className="text-xs text-gray-500 mb-1 font-medium">Excerpt:</p>
                                                        <p className="text-sm text-gray-700 italic">
                                                            "{sourceItem.excerpt}"
                                                        </p>
                                                    </div>
                                                )}
                                            </div>
                                            
                                            {(sourceItem.source.url || sourceItem.source.doi) && (
                                                <div className="flex flex-col gap-2">
                                                    {sourceItem.source.url && (
                                                        <a
                                                            href={sourceItem.source.url}
                                                            target="_blank"
                                                            rel="noopener noreferrer"
                                                            className="flex items-center gap-1 text-blue-600 hover:text-blue-800 text-sm"
                                                        >
                                                            <ExternalLink className="w-4 h-4" />
                                                            URL
                                                        </a>
                                                    )}
                                                    {sourceItem.source.doi && (
                                                        <a
                                                            href={`https://doi.org/${sourceItem.source.doi}`}
                                                            target="_blank"
                                                            rel="noopener noreferrer"
                                                            className="flex items-center gap-1 text-blue-600 hover:text-blue-800 text-sm"
                                                        >
                                                            <ExternalLink className="w-4 h-4" />
                                                            DOI
                                                        </a>
                                                    )}
                                                </div>
                                            )}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </div>
                    )}

                    {/* Metadata */}
                    <div className="bg-white rounded-lg shadow p-6">
                        <h2 className="text-xl font-semibold text-gray-900 mb-4">Metadata</h2>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                            <div className="bg-gray-50 rounded-lg p-4">
                                <div className="text-xs text-gray-500 mb-1">Status</div>
                                <div className="text-sm font-semibold text-gray-800 uppercase">
                                    {claim.status}
                                </div>
                            </div>
                            <div className="bg-gray-50 rounded-lg p-4">
                                <div className="text-xs text-gray-500 mb-1">Created At</div>
                                <div className="text-sm text-gray-800">
                                    {new Date(claim.created_at).toLocaleString()}
                                </div>
                            </div>
                            <div className="bg-gray-50 rounded-lg p-4">
                                <div className="text-xs text-gray-500 mb-1">Updated At</div>
                                <div className="text-sm text-gray-800">
                                    {new Date(claim.updated_at).toLocaleString()}
                                </div>
                            </div>
                            {claim.normalized_text && (
                                <div className="bg-gray-50 rounded-lg p-4">
                                    <div className="text-xs text-gray-500 mb-1">Normalized Text</div>
                                    <div className="text-sm text-gray-800 truncate">
                                        {claim.normalized_text}
                                    </div>
                                </div>
                            )}
                        </div>
                    </div>

                    {/* Info Box */}
                    <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 flex items-start gap-3">
                        <Info className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />
                        <div>
                            <p className="text-sm text-blue-800">
                                This claim was verified using AI and scientific sources. 
                                The confidence score indicates the reliability of the verification result.
                            </p>
                        </div>
                    </div>
                </div>
            </main>
        </div>
    );
};

export default AdminClaimDetail;
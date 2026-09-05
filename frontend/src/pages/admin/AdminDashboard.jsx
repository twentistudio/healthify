import { Activity, BarChart3, BookOpen, CheckCircle2, Clock, FileText, LogOut, Scale, Search, TrendingUp } from 'lucide-react';
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import Toast from '../../components/Toast';

const BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api';

const AdminDashboard = () => {
    const navigate = useNavigate();
    const [user, setUser] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");
    const [toast, setToast] = useState(null);
    const [stats, setStats] = useState({
        total_claims: 0,
        pending_claims: 0,
        approved_claims: 0,
        rejected_claims: 0
    });
    const [recentActivity, setRecentActivity] = useState([]);

    const showToast = (message, type = 'success') => {
        setToast({ message, type });
    };

    useEffect(() => {
        const token = localStorage.getItem('adminToken');
        const userData = localStorage.getItem('adminUser');

        if (!token || !userData) {
            navigate('/admin/login');
            return;
        }

        setUser(JSON.parse(userData));
        fetchDashboardData(token);
    }, [navigate]);

    const fetchDashboardData = async (token) => {
        try {
            const response = await fetch(`${BASE_URL}/admin/dashboard/stats/`, {
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
                throw new Error('Failed to fetch dashboard data');
            }

            const data = await response.json();
            setStats(data.stats);
            setRecentActivity(data.recent_activity);
            setLoading(false);
        } catch (err) {
            console.error('Error fetching dashboard data:', err);
            setError(err.message);
            setLoading(false);
        }
    };

    const handleLogout = async () => {
        const token = localStorage.getItem('adminToken');

        try {
            await fetch(`${BASE_URL}/admin/logout/`, {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${token}`,
                    'Content-Type': 'application/json'
                }
            });

            // Show logout success message
            showToast('Successfully logged out. See you soon! 👋', 'success');

            // Clear storage and redirect after delay
            setTimeout(() => {
                localStorage.removeItem('adminToken');
                localStorage.removeItem('adminRefreshToken');
                localStorage.removeItem('adminUser');
                navigate('/admin/login');
            }, 1500);
        } catch (error) {
            console.error('Error during logout:', error);
            // Still logout even if request fails
            showToast('Logged out successfully', 'success');
            setTimeout(() => {
                localStorage.removeItem('adminToken');
                localStorage.removeItem('adminRefreshToken');
                localStorage.removeItem('adminUser');
                navigate('/admin/login');
            }, 1500);
        }
    };

    const formatTimeAgo = (isoString) => {
        const date = new Date(isoString);
        const now = new Date();
        const secondsAgo = Math.floor((now - date) / 1000);

        if (secondsAgo < 60) return `${secondsAgo} seconds ago`;
        if (secondsAgo < 3600) return `${Math.floor(secondsAgo / 60)} minutes ago`;
        if (secondsAgo < 86400) return `${Math.floor(secondsAgo / 3600)} hours ago`;
        return `${Math.floor(secondsAgo / 86400)} days ago`;
    };

    if (loading) {
        return (
            <div className="flex items-center justify-center min-h-screen">
                <div className="text-center">
                    <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600 mx-auto mb-4"></div>
                    <p className="text-gray-600">Loading dashboard...</p>
                </div>
            </div>
        );
    }

    if (error) {
        return (
            <div className="flex items-center justify-center min-h-screen">
                <div className="bg-red-50 border border-red-200 text-red-700 px-6 py-4 rounded-lg">
                    {error}
                </div>
            </div>
        );
    }

    return (
        <div className="min-h-screen bg-gray-50">
            {/* Toast Notification */}
            {toast && (
                <Toast 
                    message={toast.message}
                    type={toast.type}
                    onClose={() => setToast(null)}
                />
            )}

            {/* Header */}
            <header className="bg-gradient-to-r from-blue-600 to-cyan-600 shadow-lg">
                <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
                    <div className="flex justify-between items-center">
                        <div>
                            <div className="flex items-center gap-3 mb-2">
                                <Activity className="w-8 h-8 text-white" />
                                <h1 className="text-2xl sm:text-3xl font-bold text-white">
                                    Admin Dashboard
                                </h1>
                            </div>
                            <p className="text-sm text-blue-100">
                                Welcome back, <span className="font-semibold text-white">{user?.username}</span>! 
                            </p>
                        </div>
                        <button
                            onClick={handleLogout}
                            className="inline-flex items-center gap-2 px-4 py-2 bg-white/20 hover:bg-white/30 text-white rounded-lg transition backdrop-blur-sm border border-white/20"
                        >
                            <LogOut className="w-4 h-4" />
                            Logout
                        </button>
                    </div>
                </div>
            </header>

            {/* Rest of dashboard content... */}
            <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
                {/* Stats Cards */}
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">
                    <StatCard 
                        title="Total Claims"
                        value={stats.total_claims}
                        icon={<BarChart3 className="w-6 h-6" />}
                        color="blue"
                        trend="+12%"
                    />
                    <StatCard 
                        title="Pending Disputes"
                        value={stats.pending_disputes}
                        icon={<Clock className="w-6 h-6" />}
                        color="yellow"
                        trend="-5%"
                    />
                    <StatCard
                        title="Total Sources"
                        value={stats.total_sources}
                        icon={<BookOpen className="w-6 h-6" />}
                        color="green"
                        trend="+8%"
                    />
                    <StatCard
                        title="Verified Claims"
                        value={stats.verified_claims}
                        icon={<CheckCircle2 className="w-6 h-6" />}
                        color="teal"
                        trend="+15%"
                    />
                </div>

                {/* Quick Actions */}
                <div className="bg-white rounded-2xl shadow-xl border border-slate-100 p-6 sm:p-8 mb-8">
                    <h2 className="text-xl font-bold text-gray-800 mb-6 flex items-center gap-2">
                        <TrendingUp className="w-5 h-5 text-blue-600" />
                        Quick Actions
                    </h2>
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                        <ActionButton 
                            label="Manage Claims"
                            description="View and manage all claims"
                            icon={<FileText className="w-6 h-6" />}
                            color="blue"
                            onClick={() => navigate('/admin/claims')}
                        />
                        <ActionButton
                            label="Review Disputes"
                            description="Handle user disputes"
                            icon={<Scale className="w-6 h-6" />}
                            color="teal"
                            onClick={() => navigate('/admin/disputes')}
                        />
                        <ActionButton
                            label="Source Management"
                            description="Add or remove sources"
                            icon={<Search className="w-6 h-6" />}
                            color="green"
                            onClick={() => navigate('/admin/sources')}
                        />
                    </div>
                </div>

                {/* Recent Activity */}
                <div className="bg-white rounded-2xl shadow-xl border border-slate-100 p-6 sm:p-8">
                    <h2 className="text-xl font-bold text-gray-800 mb-6 flex items-center gap-2">
                        <Activity className="w-5 h-5 text-blue-600" />
                        Recent Activity
                    </h2>
                    {recentActivity.length === 0 ? (
                        <p className="text-gray-500 text-center py-8">No recent activity.</p>
                    ) : (
                        <div className="space-y-3">
                            {recentActivity.map((activity, index) => (
                                <ActivityItem
                                    key={index}
                                    text={activity.text}
                                    time={formatTimeAgo(activity.timestamp)}
                                    type={activity.type}
                                />
                            ))}
                        </div>
                    )}
                </div>
            </main>
        </div>
    );
};

// Stat Card Component
const StatCard = ({ title, value, icon, color, trend }) => {
    const colorClasses = {
        blue: 'from-blue-500 to-blue-600',
        yellow: 'from-yellow-500 to-orange-500',
        green: 'from-green-500 to-emerald-600',
        teal: 'from-teal-500 to-cyan-600'
    };

    const bgColorClasses = {
        blue: 'bg-blue-50',
        yellow: 'bg-yellow-50',
        green: 'bg-green-50',
        teal: 'bg-teal-50'
    };

    const textColorClasses = {
        blue: 'text-blue-600',
        yellow: 'text-yellow-600',
        green: 'text-green-600',
        teal: 'text-teal-600'
    };

    return (
        <div className="bg-white rounded-xl shadow-lg border border-slate-100 p-6 hover:shadow-xl transition-shadow">
            <div className="flex items-start justify-between mb-4">
                <div className={`${bgColorClasses[color]} p-3 rounded-lg`}>
                    <div className={textColorClasses[color]}>
                        {icon}
                    </div>
                </div>
                {trend && (
                    <span className={`text-xs font-semibold px-2 py-1 rounded-full ${trend.startsWith('+') ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
                        {trend}
                    </span>
                )}
            </div>
            <p className="text-sm text-gray-600 mb-1">{title}</p>
            <p className="text-3xl font-bold text-gray-900">{value}</p>
        </div>
    );
};

// Action Button Component
const ActionButton = ({ label, description, icon, color, onClick }) => {
    const colorClasses = {
        blue: 'from-blue-500 to-blue-600 hover:from-blue-600 hover:to-blue-700',
        teal: 'from-teal-500 to-cyan-600 hover:from-teal-600 hover:to-cyan-700',
        green: 'from-green-500 to-emerald-600 hover:from-green-600 hover:to-emerald-700'
    };

    return (
        <button
            onClick={onClick}
            className={`bg-gradient-to-br ${colorClasses[color]} text-white rounded-xl p-5 text-left transition-all hover:shadow-lg transform hover:-translate-y-1`}
        >
            <div className="mb-3 bg-white/20 w-12 h-12 rounded-lg flex items-center justify-center">
                {icon}
            </div>
            <h3 className="font-bold text-lg mb-1">{label}</h3>
            <p className="text-sm text-white/80">{description}</p>
        </button>
    );
};

// Activity Item Component
const ActivityItem = ({ text, time, type }) => {
    const getIcon = () => {
        switch (type) {
            case 'claim':
                return <FileText className="w-5 h-5 text-blue-600" />;
            case 'dispute':
                return <Scale className="w-5 h-5 text-orange-600" />;
            case 'source':
                return <BookOpen className="w-5 h-5 text-green-600" />;
            default:
                return <Activity className="w-5 h-5 text-gray-600" />;
        }
    };

    const getBgColor = () => {
        switch (type) {
            case 'claim':
                return 'bg-blue-50';
            case 'dispute':
                return 'bg-orange-50';
            case 'source':
                return 'bg-green-50';
            default:
                return 'bg-gray-50';
        }
    };

    return (
        <div className="flex items-start gap-4 p-3 rounded-lg hover:bg-gray-50 transition-colors">
            <div className={`${getBgColor()} p-2 rounded-lg`}>
                {getIcon()}
            </div>
            <div className="flex-1">
                <p className="text-gray-900 text-sm font-medium">{text}</p>
                <p className="text-xs text-gray-500 mt-1 flex items-center gap-1">
                    <Clock className="w-3 h-3" />
                    {time}
                </p>
            </div>
        </div>
    );
};

export default AdminDashboard;
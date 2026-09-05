import { AlertCircle, ArrowLeft, Loader2, Lock, ShieldCheck, User } from 'lucide-react';
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import Toast from '../../components/Toast';
import '../../index.css';

const BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api';

const AdminLogin = () => {
    const [username, setUsername] = useState("");
    const [password, setPassword] = useState("");
    const [error, setError] = useState("");
    const [loading, setLoading] = useState(false);
    const [toast, setToast] = useState(null);
    const navigate = useNavigate();

    const showToast = (message, type = 'success') => {
        setToast({ message, type });
    };

    const handleLogin = async (e) => {
        e.preventDefault();
        setLoading(true);
        setError("");

        try {
            const response = await fetch(`${BASE_URL}/admin/login/`,{
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ username, password }),
            });

            const data = await response.json();

            if (response.ok && data.access) {
                // Menyimpan token dan user info
                localStorage.setItem('adminToken', data.access);
                localStorage.setItem('adminRefreshToken', data.refresh);
                localStorage.setItem('adminUser', JSON.stringify(data.user));

                // Show success toast
                showToast(`Welcome back, ${data.user.username}! 🎉`, 'success');

                // Redirect setelah delay singkat
                setTimeout(() => {
                    navigate('/admin/dashboard');
                }, 1500);
            } else {
                setError(data.message || 'Login failed. Please check your credentials.');
                showToast('Invalid username or password', 'error');
            }
        } catch (error) {
            setError('Network error. Please check your connection.');
            showToast('Network error. Please try again later.', 'error');
            console.error('Error during login:', error);
        } finally{
            setLoading(false);
        }
    };

    return (
        <div className="min-h-screen bg-gradient-to-br from-blue-600 via-cyan-600 to-teal-500 flex items-center justify-center p-4">
            {/* Toast Notification */}
            {toast && (
                <Toast 
                    message={toast.message}
                    type={toast.type}
                    onClose={() => setToast(null)}
                />
            )}

            <div className="bg-white rounded-2xl shadow-2xl p-8 w-full max-w-md backdrop-blur-sm">
                {/* Header */}
                <div className="text-center mb-8">
                    <div className="inline-flex items-center justify-center w-16 h-16 bg-gradient-to-br from-blue-500 to-cyan-600 rounded-full mb-4">
                        <ShieldCheck className="w-8 h-8 text-white" />
                    </div>
                    <h1 className="text-3xl font-bold text-gray-800 mb-2">
                        Admin Login
                    </h1>
                    <p className="text-gray-600">
                        Healthify Admin Panel
                    </p>
                </div>

                {/* Error Message */}
                {error && (
                    <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg mb-6 flex items-start gap-2">
                        <AlertCircle className="w-5 h-5 flex-shrink-0 mt-0.5" />
                        <span>{error}</span>
                    </div>
                )}

                {/* Login Form */}
                <form onSubmit={handleLogin} className="space-y-6">
                    <div>
                        <label className="block text-sm font-semibold text-gray-700 mb-2">
                            Username
                        </label>
                        <div className="relative">
                            <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                                <User className="w-5 h-5 text-gray-400" />
                            </div>
                            <input 
                                type="text" 
                                value={username}
                                onChange={(e) => setUsername(e.target.value)}
                                className="w-full pl-10 pr-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent transition"
                                placeholder="Enter your username"
                                required
                                disabled={loading}
                            />
                        </div>
                    </div>

                    <div>
                        <label className="block text-sm font-semibold text-gray-700 mb-2">
                            Password
                        </label>
                        <div className="relative">
                            <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                                <Lock className="w-5 h-5 text-gray-400" />
                            </div>
                            <input 
                                type="password" 
                                value={password}
                                onChange={(e) => setPassword(e.target.value)}
                                className="w-full pl-10 pr-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent transition"
                                placeholder="Enter your password"
                                required
                                disabled={loading}
                            />
                        </div>
                    </div>

                    <button
                        type="submit"
                        disabled={loading}
                        className="w-full bg-gradient-to-r from-blue-600 to-cyan-600 hover:from-blue-700 hover:to-cyan-700 text-white font-semibold py-3 rounded-lg transition duration-200 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2 shadow-lg hover:shadow-xl"
                    >
                        {loading ? (
                            <>
                                <Loader2 className="w-5 h-5 animate-spin" />
                                Logging in...
                            </>
                        ) : (
                            <>
                                <ShieldCheck className="w-5 h-5" />
                                Login
                            </>
                        )}
                    </button>
                </form>

                {/* Back to Home */}
                <div className="mt-6 text-center">
                    <button
                        onClick={() => navigate('/')}
                        className="inline-flex items-center gap-1 text-blue-600 hover:text-blue-700 text-sm font-medium transition"
                        disabled={loading}
                    >
                        <ArrowLeft className="w-4 h-4" />
                        Back to Home
                    </button>
                </div>
            </div>
        </div>
    );
};

export default AdminLogin;
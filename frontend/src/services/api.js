const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api';

console.log('🌐 API Base URL:', API_BASE_URL);

// Handle API response dan error
const handleResponse = async (response) => {
    if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.error || `HTTP error! status: ${response.status}`);
    }
    return response.json();
}

/**
 *Verify claim
 *POST /api/verify 
 */

export const verifyClaim = async (claimText, options = {}) => {
    const body = { text: claimText };

    if (options.force_refresh) {
        body._force_refresh = true;
        body._timestamp = Date.now();
    }
    try {
    console.log('API URL:', API_BASE_URL); // Debugging line
    const response = await fetch(`${API_BASE_URL}/verify/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    });

    return await handleResponse(response);
  } catch (error) {
    console.error('Error verifying claim:', error);
    throw error;
  }
};

/**
 * GET Claim Detail by ID
 * GET /api/claims/{id}
 * @param {number} 
 * @returns {Promise}
 */

export const getClaimDetail = async (claimId) => {
    try {
        const response = await fetch(`${API_BASE_URL}/claims/${claimId}`, {
            method: 'GET',
            headers: {
                'Content-Type': 'application/json',
            },
        });

        return await handleResponse(response);
    } catch (error) {
        console.error('Error fetching claim detail:', error);
        throw error;
    }
};

/**
 * Translate verification result (label + summary)
 * POST /api/translate/
 * @param {{ label: string, summary: string, target_language: 'en' | 'id' }} payload
 * @returns {Promise}
 */
export const translateVerificationResult = async (payload) => {
    try {
        const response = await fetch(`${API_BASE_URL}/translate/`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(payload),
        });

        return await handleResponse(response);
    } catch (error) {
        console.error('Error translating verification result:', error);
        throw error;
    }
};

/** 
 * GET ALL Claims (HISTORY)
 * GET /api/claims
 * @returns {Promise}
 */
export const getAllClaims = async () => {
    try {
        const response = await fetch(`${API_BASE_URL}/claims/`, {
            method: 'GET',
            headers: {
                'Content-Type': 'application/json',
            },
        });

        return await handleResponse(response);
    } catch (error) {
        console.error('Error fetching all claims:', error);
        throw error;
    }
};


/** * Create Dispute
 * POST /api/disputes/create
 * @param {Object} disputeData
 * @returns {Promise}
 */
export
const createDispute = async (disputeData) => {
    try {
        const formData = new FormData();

        // Menambahkan claim_id atau claim_text
        if (disputeData.claim_id) {
            formData.append('claim_id', disputeData.claim_id);
        }
        if (disputeData.claim_text) {
            formData.append('claim_text', disputeData.claim_text);
        }

        // Data wajib
        formData.append('reason', disputeData.reason);
        formData.append('reporter_name', disputeData.reporter_name || '');
        formData.append('reporter_email', disputeData.reporter_email || '');

        if (disputeData.supporting_doi) {
            formData.append('supporting_doi', disputeData.supporting_doi);
        }
        if (disputeData.supporting_file) {
            formData.append('supporting_file', disputeData.supporting_file);
        }
        if (disputeData.supporting_url) {
            formData.append('supporting_url', disputeData.supporting_url);
        }

        const response = await fetch(`${API_BASE_URL}/disputes/create/`, {
            method: 'POST',
            body: formData,
        });

        return await handleResponse(response);
    } catch (error) {
        console.error('Error creating dispute:', error);
        throw error;
    }
};

export default{
    verifyClaim, 
    handleResponse,
    createDispute,
    getClaimDetail,
    getAllClaims,
    translateVerificationResult,
};
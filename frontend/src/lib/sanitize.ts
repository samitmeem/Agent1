/**
 * XSS sanitization utilities using DOMPurify.
 *
 * CRITICAL: All user-generated content and LLM responses MUST be sanitized
 * before rendering to prevent XSS attacks.
 */
import DOMPurify from 'dompurify';

/**
 * Sanitize HTML content to prevent XSS attacks.
 *
 * Removes all script tags, event handlers, and dangerous attributes
 * while preserving safe formatting (bold, italic, links, etc.).
 *
 * @param dirty - Untrusted HTML content (from user input or LLM)
 * @returns Safe HTML string with XSS vectors removed
 */
export function sanitizeHtml(dirty: string): string {
        return DOMPurify.sanitize(dirty, {
                ALLOWED_TAGS: ['b', 'i', 'em', 'strong', 'a', 'p', 'br', 'ul', 'ol', 'li', 'code', 'pre'],
                ALLOWED_ATTR: ['href', 'target', 'rel'],
                ALLOW_DATA_ATTR: false,
        });
}

/**
 * Sanitize plain text content (escape HTML entities).
 *
 * Use this for content that should be displayed as-is with no HTML.
 *
 * @param text - Untrusted text content
 * @returns HTML-escaped safe string
 */
export function sanitizeText(text: string): string {
        return DOMPurify.sanitize(text, { ALLOWED_TAGS: [] });
}

const createForm = document.getElementById('create-form');
const topicInput = document.getElementById('topic-input');
const createButton = document.getElementById('create-button');
const progressContainer = document.getElementById('progress-container');
const statusText = document.getElementById('status-text');
const coursesSection = document.getElementById('courses-section');
const coursesList = document.getElementById('courses-list');

// Generate a random session ID for this browser session
const sessionId = 'session-' + Math.random().toString(36).substring(2, 15);

// Load saved courses on page load
async function loadCourses() {
    try {
        const response = await fetch('/api/courses');
        if (!response.ok) return;
        const courses = await response.json();
        if (courses.length === 0) return;

        coursesList.innerHTML = '';
        for (const course of courses) {
            const li = document.createElement('li');
            li.className = 'course-item';

            const a = document.createElement('a');
            a.href = '/course.html?file=' + encodeURIComponent(course.filename);
            a.className = 'course-link';

            const title = document.createElement('span');
            title.className = 'course-title';
            title.textContent = course.title;

            const date = document.createElement('span');
            date.className = 'course-date';
            if (course.created_at) {
                const d = new Date(course.created_at);
                date.textContent = d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
            }

            a.appendChild(title);
            a.appendChild(date);
            li.appendChild(a);
            coursesList.appendChild(li);
        }
        coursesSection.classList.remove('hidden');
        document.querySelector('.container').classList.add('has-courses');
    } catch (e) {
        console.error('Failed to load courses:', e);
    }
}

loadCourses();

function showProgress() {
    createForm.classList.add('hidden'); // Optionally hide form, or just disable
    topicInput.disabled = true;
    createButton.disabled = true;
    createButton.innerHTML = 'Building...';
    progressContainer.classList.remove('hidden');
}

function updateStatus(text) {
    statusText.textContent = text;
    
    // Simple logic to highlight steps based on text content
    document.querySelectorAll('.step').forEach(s => s.classList.remove('active'));
    
    if (text.toLowerCase().includes('research')) {
        document.getElementById('step-researcher').classList.add('active');
    } else if (text.toLowerCase().includes('judge') || text.toLowerCase().includes('evaluating')) {
        document.getElementById('step-judge').classList.add('active');
    } else if (text.toLowerCase().includes('writ') || text.toLowerCase().includes('build')) {
        document.getElementById('step-builder').classList.add('active');
    }
}

createForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const topic = topicInput.value.trim();
    if (!topic) return;

    showProgress();

    try {
        const response = await fetch('/api/chat_stream', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                message: `Create a comprehensive course on: ${topic}`,
                session_id: sessionId
            })
        });

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                if (!line.trim()) continue;
                try {
                    const data = JSON.parse(line);
                    if (data.type === 'progress') {
                        updateStatus(data.text);
                    } else if (data.type === 'result') {
                        // Redirect to course page
                        if (data.filename) {
                            window.location.href = '/course.html?file=' + encodeURIComponent(data.filename);
                        } else {
                            // Fallback: store in localStorage
                            localStorage.setItem('currentCourse', data.text);
                            window.location.href = '/course.html';
                        }
                        return;
                    }
                } catch (e) {
                    console.error('Error parsing JSON:', e, line);
                }
            }
        }

    } catch (error) {
        console.error('Error:', error);
        statusText.textContent = 'Something went wrong. Please refresh and try again.';
        // Re-enable form if needed, or just let them refresh
    }
});
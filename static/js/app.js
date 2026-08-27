/**
 * Pixel Vault — Frontend Application
 * Bitcask CAS Photo Cloud Client
 */

// ============================================================================
// Global Image Error Fallback Handler (Prevents Infinite 404 Retry Loops)
// ============================================================================
window.__handleImgError = function(img, photoId) {
  if (!img.dataset.triedFallback) {
    img.dataset.triedFallback = '1';
    img.src = '/photos/' + photoId + '/file';
  } else {
    img.onerror = null;
    img.classList.add('img-missing');
    img.src = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 200 200' fill='none'%3E%3Crect width='200' height='200' fill='%230f172a'/%3E%3Cpath d='M70 80l15-15h30l15 15h20v60H50V80h20z' stroke='%23334155' stroke-width='4' stroke-linejoin='round'/%3E%3Ccircle cx='100' cy='110' r='20' stroke='%23334155' stroke-width='4'/%3E%3Cline x1='60' y1='60' x2='140' y2='140' stroke='%23ef4444' stroke-width='4' stroke-linecap='round'/%3E%3Ctext x='100' y='165' fill='%2394a3b8' font-family='sans-serif' font-size='12' font-weight='bold' text-anchor='middle'%3EBlob Missing%3C/text%3E%3C/svg%3E";
  }
};

// ============================================================================
// 1. Toast Notification Manager
// ============================================================================
class ToastManager {
  constructor() {
    this.container = document.getElementById('toast-container');
  }

  show(message, type = 'info', duration = 3500) {
    if (!this.container) return;

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;

    let iconSvg = '';
    if (type === 'success') {
      iconSvg = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><polyline points="20 6 9 17 4 12"/></svg>';
    } else if (type === 'error') {
      iconSvg = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>';
    } else if (type === 'warning') {
      iconSvg = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';
    } else {
      iconSvg = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>';
    }

    toast.innerHTML = `
      <div class="toast-icon">${iconSvg}</div>
      <div class="toast-msg">${this._escapeHtml(message)}</div>
    `;

    this.container.appendChild(toast);

    setTimeout(() => {
      toast.classList.add('removing');
      setTimeout(() => toast.remove(), 260);
    }, duration);
  }

  _escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }
}

// ============================================================================
// 2. API Client
// ============================================================================
class APIClient {
  static async checkHealth() {
    try {
      const res = await fetch('/health');
      return res.ok;
    } catch {
      return false;
    }
  }

  static async listPhotos(offset = 0, limit = 100) {
    const res = await fetch(`/photos?offset=${offset}&limit=${limit}`);
    if (!res.ok) throw new Error(`Failed to load photos: ${res.statusText}`);
    return await res.json();
  }

  static async getPhotoMeta(photoId) {
    const res = await fetch(`/photos/${photoId}`);
    if (!res.ok) throw new Error(`Photo ${photoId} not found`);
    return await res.json();
  }

  static async uploadPhoto(file) {
    const formData = new FormData();
    formData.append('file', file);

    const res = await fetch('/photos', {
      method: 'POST',
      body: formData,
    });

    if (!res.ok) {
      let errDetail = 'Upload failed';
      try {
        const errJson = await res.json();
        if (errJson.detail) errDetail = errJson.detail;
      } catch {
        errDetail = res.statusText;
      }
      throw new Error(errDetail);
    }

    const data = await res.json();
    // 201 = New Blob Created, 200 = CAS Duplicate Hit
    return { data, isDuplicate: res.status === 200, status: res.status };
  }

  static async compactSegments() {
    const res = await fetch('/admin/compact', { method: 'POST' });
    if (!res.ok) throw new Error(`Compaction failed: ${res.statusText}`);
    return await res.json();
  }

  static async saveSnapshot() {
    const res = await fetch('/admin/snapshot', { method: 'POST' });
    if (!res.ok) throw new Error(`Snapshot failed: ${res.statusText}`);
    return await res.json();
  }
}

// ============================================================================
// 3. Gallery Manager
// ============================================================================
class GalleryManager {
  constructor(app) {
    this.app = app;
    this.photos = [];
    this.filteredPhotos = [];
    this.searchQuery = '';
    this.activeCameraFilter = 'ALL';
    this.sortMode = 'taken_desc';

    this.streamEl = document.getElementById('timeline-stream');
    this.skeletonsEl = document.getElementById('gallery-skeletons');
    this.emptyStateEl = document.getElementById('empty-state');
    this.noResultsEl = document.getElementById('no-results');
    this.photoCountBadge = document.getElementById('photo-count-text');
    this.cameraFiltersEl = document.getElementById('camera-filters');
    this.sortSelect = document.getElementById('sort-select');

    this._initEvents();
  }

  _initEvents() {
    if (this.sortSelect) {
      this.sortSelect.addEventListener('change', (e) => {
        this.sortMode = e.target.value;
        this.applyFiltersAndRender();
      });
    }

    if (this.cameraFiltersEl) {
      this.cameraFiltersEl.addEventListener('click', (e) => {
        const chip = e.target.closest('.filter-chip');
        if (!chip) return;
        const camera = chip.dataset.camera;
        this.setCameraFilter(camera);
      });
    }

    const resetBtn = document.getElementById('reset-search-btn');
    if (resetBtn) {
      resetBtn.addEventListener('click', () => {
        this.app.clearSearch();
      });
    }
  }

  async loadPhotos() {
    try {
      this.skeletonsEl.classList.remove('hidden');
      this.emptyStateEl.classList.add('hidden');
      this.noResultsEl.classList.add('hidden');

      const response = await APIClient.listPhotos(0, 200);
      this.photos = response.photos || [];

      this.updateCameraChips();
      this.applyFiltersAndRender();
    } catch (err) {
      this.app.toast.show(err.message, 'error');
    } finally {
      this.skeletonsEl.classList.add('hidden');
    }
  }

  setSearchQuery(query) {
    this.searchQuery = (query || '').trim().toLowerCase();
    this.applyFiltersAndRender();
  }

  setCameraFilter(camera) {
    this.activeCameraFilter = camera;
    this.cameraFiltersEl.querySelectorAll('.filter-chip').forEach(chip => {
      chip.classList.toggle('active', chip.dataset.camera === camera);
    });
    this.applyFiltersAndRender();
  }

  updateCameraChips() {
    if (!this.cameraFiltersEl) return;
    const cameras = new Set();
    this.photos.forEach(p => {
      if (p.camera && p.camera.trim()) {
        cameras.add(p.camera.trim());
      }
    });

    let html = `<button class="filter-chip ${this.activeCameraFilter === 'ALL' ? 'active' : ''}" data-camera="ALL">All Cameras</button>`;
    cameras.forEach(cam => {
      const isSelected = this.activeCameraFilter === cam;
      html += `<button class="filter-chip ${isSelected ? 'active' : ''}" data-camera="${this._escapeHtml(cam)}">${this._escapeHtml(cam)}</button>`;
    });

    this.cameraFiltersEl.innerHTML = html;
  }

  applyFiltersAndRender() {
    let result = [...this.photos];

    // Filter by camera
    if (this.activeCameraFilter !== 'ALL') {
      result = result.filter(p => p.camera === this.activeCameraFilter);
    }

    // Filter by search query
    if (this.searchQuery) {
      result = result.filter(p => {
        const cam = (p.camera || '').toLowerCase();
        const hash = (p.content_hash || '').toLowerCase();
        const id = (p.photo_id || '').toLowerCase();
        const taken = (p.taken_at || '').toLowerCase();
        return cam.includes(this.searchQuery) ||
               hash.includes(this.searchQuery) ||
               id.includes(this.searchQuery) ||
               taken.includes(this.searchQuery);
      });
    }

    // Sort photos
    result.sort((a, b) => {
      const dateA = a.taken_at ? new Date(a.taken_at).getTime() : 0;
      const dateB = b.taken_at ? new Date(b.taken_at).getTime() : 0;
      const uploadA = a.uploaded_at ? new Date(a.uploaded_at).getTime() : 0;
      const uploadB = b.uploaded_at ? new Date(b.uploaded_at).getTime() : 0;

      if (this.sortMode === 'taken_desc') {
        return (dateB || uploadB) - (dateA || uploadA);
      } else if (this.sortMode === 'taken_asc') {
        return (dateA || uploadA) - (dateB || uploadB);
      } else if (this.sortMode === 'upload_desc') {
        return uploadB - uploadA;
      }
      return 0;
    });

    this.filteredPhotos = result;
    this.render();
  }

  render() {
    // Update badge count
    if (this.photoCountBadge) {
      const count = this.photos.length;
      this.photoCountBadge.textContent = `${count} photo${count === 1 ? '' : 's'}`;
    }

    // Handle empty state vs no search results
    if (this.photos.length === 0) {
      this.streamEl.innerHTML = '';
      this.emptyStateEl.classList.remove('hidden');
      this.noResultsEl.classList.add('hidden');
      return;
    }

    this.emptyStateEl.classList.add('hidden');

    if (this.filteredPhotos.length === 0) {
      this.streamEl.innerHTML = '';
      this.noResultsEl.classList.remove('hidden');
      return;
    }

    this.noResultsEl.classList.add('hidden');

    // Group photos chronologically by Month & Year
    const groups = {};
    this.filteredPhotos.forEach(photo => {
      const dateStr = photo.taken_at || photo.uploaded_at;
      let groupKey = 'Undated';
      if (dateStr) {
        try {
          const d = new Date(dateStr);
          groupKey = d.toLocaleDateString(undefined, { year: 'numeric', month: 'long' });
        } catch {
          groupKey = 'Undated';
        }
      }
      if (!groups[groupKey]) groups[groupKey] = [];
      groups[groupKey].push(photo);
    });

    let html = '';
    for (const [groupName, photos] of Object.entries(groups)) {
      html += `
        <div class="timeline-group">
          <div class="timeline-header">
            <h3 class="timeline-title">${this._escapeHtml(groupName)}</h3>
            <span class="timeline-count">${photos.length} item${photos.length === 1 ? '' : 's'}</span>
          </div>
          <div class="photo-grid">
            ${photos.map(p => this._renderPhotoCard(p)).join('')}
          </div>
        </div>
      `;
    }

    this.streamEl.innerHTML = html;

    // Attach card click handlers
    this.streamEl.querySelectorAll('.photo-card').forEach(card => {
      card.addEventListener('click', () => {
        const photoId = card.dataset.photoId;
        this.app.lightbox.open(photoId, this.filteredPhotos);
      });
    });
  }

  _renderPhotoCard(photo) {
    const hasThumb = photo.thumbnail_paths && photo.thumbnail_paths.length > 0;
    const initialUrl = hasThumb 
      ? `/photos/${photo.photo_id}/thumbnail` 
      : `/photos/${photo.photo_id}/file`;
    const cameraText = photo.camera || 'Standard Camera';
    const dateFormatted = photo.taken_at 
      ? new Date(photo.taken_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
      : (photo.uploaded_at ? new Date(photo.uploaded_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : 'Unknown');

    return `
      <div class="photo-card" data-photo-id="${photo.photo_id}" tabindex="0" role="button" aria-label="View photo ${photo.photo_id}">
        <img class="photo-thumb" 
             src="${initialUrl}" 
             alt="${this._escapeHtml(cameraText)}"
             loading="lazy"
             onerror="window.__handleImgError(this, '${photo.photo_id}')">
        <div class="card-overlay">
          <div class="card-top-badges">
            ${photo.gps ? '<span class="stat-badge storage-pill" title="Has GPS coordinates"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg></span>' : ''}
          </div>
          <div class="card-bottom-info">
            <span class="card-camera-text">${this._escapeHtml(cameraText)}</span>
            <span class="card-date-text">${this._escapeHtml(dateFormatted)}</span>
          </div>
        </div>
      </div>
    `;
  }

  _escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }
}

// ============================================================================
// 4. Upload Manager (Desktop Drag-and-Drop & Mobile Camera/Gallery)
// ============================================================================
class UploadManager {
  constructor(app) {
    this.app = app;
    this.modalEl = document.getElementById('upload-modal');
    this.dropzoneEl = document.getElementById('dropzone-area');
    this.dragOverlayEl = document.getElementById('drag-drop-overlay');
    this.fileInputMulti = document.getElementById('file-input-multiple');
    this.fileInputCamera = document.getElementById('file-input-camera');
    this.queueSectionEl = document.getElementById('upload-queue-section');
    this.queueListEl = document.getElementById('upload-queue-list');
    this.queueCountText = document.getElementById('queue-count-text');

    this.queue = [];
    this.isUploading = false;

    this._initEvents();
  }

  _initEvents() {
    // Open Modal Buttons
    const openBtns = [
      document.getElementById('upload-btn'),
      document.getElementById('empty-upload-btn'),
      document.getElementById('mobile-fab-btn')
    ];
    openBtns.forEach(btn => {
      if (btn) btn.addEventListener('click', () => this.openModal());
    });

    // Close Modal Button
    const closeBtn = document.getElementById('close-upload-modal-btn');
    if (closeBtn) closeBtn.addEventListener('click', () => this.closeModal());
    if (this.modalEl) {
      this.modalEl.addEventListener('click', (e) => {
        if (e.target === this.modalEl) this.closeModal();
      });
    }

    // Trigger Native File Selectors
    const selectFilesBtn = document.getElementById('select-files-btn');
    if (selectFilesBtn && this.fileInputMulti) {
      selectFilesBtn.addEventListener('click', () => this.fileInputMulti.click());
    }

    const openCameraBtn = document.getElementById('open-camera-btn');
    if (openCameraBtn && this.fileInputCamera) {
      openCameraBtn.addEventListener('click', () => this.fileInputCamera.click());
    }

    if (this.fileInputMulti) {
      this.fileInputMulti.addEventListener('change', (e) => {
        if (e.target.files && e.target.files.length > 0) {
          this.handleFiles(Array.from(e.target.files));
          e.target.value = '';
        }
      });
    }

    if (this.fileInputCamera) {
      this.fileInputCamera.addEventListener('change', (e) => {
        if (e.target.files && e.target.files.length > 0) {
          this.handleFiles(Array.from(e.target.files));
          e.target.value = '';
        }
      });
    }

    // Clear completed uploads
    const clearBtn = document.getElementById('clear-completed-uploads-btn');
    if (clearBtn) {
      clearBtn.addEventListener('click', () => {
        this.queue = this.queue.filter(item => item.status === 'uploading');
        this.renderQueue();
      });
    }

    // Dropzone Events inside Modal
    if (this.dropzoneEl) {
      ['dragenter', 'dragover'].forEach(eventName => {
        this.dropzoneEl.addEventListener(eventName, (e) => {
          e.preventDefault();
          this.dropzoneEl.classList.add('drag-over');
        });
      });

      ['dragleave', 'drop'].forEach(eventName => {
        this.dropzoneEl.addEventListener(eventName, (e) => {
          e.preventDefault();
          this.dropzoneEl.classList.remove('drag-over');
        });
      });

      this.dropzoneEl.addEventListener('drop', (e) => {
        if (e.dataTransfer && e.dataTransfer.files.length > 0) {
          this.handleFiles(Array.from(e.dataTransfer.files));
        }
      });
    }

    // Global Window Drag-and-Drop
    let dragCounter = 0;
    window.addEventListener('dragenter', (e) => {
      e.preventDefault();
      dragCounter++;
      if (this.dragOverlayEl && e.dataTransfer.types.includes('Files')) {
        this.dragOverlayEl.classList.remove('hidden');
      }
    });

    window.addEventListener('dragleave', (e) => {
      e.preventDefault();
      dragCounter--;
      if (dragCounter <= 0 && this.dragOverlayEl) {
        this.dragOverlayEl.classList.add('hidden');
        dragCounter = 0;
      }
    });

    window.addEventListener('dragover', (e) => e.preventDefault());

    window.addEventListener('drop', (e) => {
      e.preventDefault();
      dragCounter = 0;
      if (this.dragOverlayEl) this.dragOverlayEl.classList.add('hidden');
      if (e.dataTransfer && e.dataTransfer.files.length > 0) {
        this.openModal();
        this.handleFiles(Array.from(e.dataTransfer.files));
      }
    });
  }

  openModal() {
    if (this.modalEl) this.modalEl.classList.remove('hidden');
  }

  closeModal() {
    if (this.modalEl) this.modalEl.classList.add('hidden');
  }

  handleFiles(files) {
    const validFiles = files.filter(f => f.type === 'image/jpeg' || f.type === 'image/png' || f.name.match(/\.(jpe?g|png)$/i));
    if (validFiles.length === 0) {
      this.app.toast.show('Please choose JPEG or PNG image files.', 'warning');
      return;
    }

    validFiles.forEach(file => {
      this.queue.push({
        id: Math.random().toString(36).substring(2, 9),
        file,
        name: file.name,
        size: this._formatBytes(file.size),
        status: 'pending',
        isDuplicate: false,
        error: null,
      });
    });

    this.renderQueue();
    this.processQueue();
  }

  async processQueue() {
    if (this.isUploading) return;
    this.isUploading = true;

    while (true) {
      const nextItem = this.queue.find(item => item.status === 'pending');
      if (!nextItem) break;

      nextItem.status = 'uploading';
      this.renderQueue();

      try {
        const result = await APIClient.uploadPhoto(nextItem.file);
        nextItem.isDuplicate = result.isDuplicate;
        nextItem.status = result.isDuplicate ? 'dedup' : 'success';

        if (result.isDuplicate) {
          this.app.toast.show(`"${nextItem.name}" already in CAS store (deduplicated).`, 'info');
        } else {
          this.app.toast.show(`Uploaded "${nextItem.name}" successfully!`, 'success');
        }
      } catch (err) {
        nextItem.status = 'error';
        nextItem.error = err.message;
        this.app.toast.show(`Failed to upload ${nextItem.name}: ${err.message}`, 'error');
      }

      this.renderQueue();
    }

    this.isUploading = false;
    // Reload gallery to display new items
    this.app.gallery.loadPhotos();
  }

  renderQueue() {
    if (!this.queueSectionEl || !this.queueListEl) return;

    if (this.queue.length === 0) {
      this.queueSectionEl.classList.add('hidden');
      return;
    }

    this.queueSectionEl.classList.remove('hidden');
    if (this.queueCountText) {
      this.queueCountText.textContent = `Upload Queue (${this.queue.length})`;
    }

    let html = '';
    this.queue.forEach(item => {
      let statusBadge = '';
      if (item.status === 'uploading') {
        statusBadge = '<span class="queue-status-chip uploading">Streaming...</span>';
      } else if (item.status === 'dedup') {
        statusBadge = '<span class="queue-status-chip dedup">CAS Dedup Hit</span>';
      } else if (item.status === 'success') {
        statusBadge = '<span class="queue-status-chip success">Stored</span>';
      } else if (item.status === 'error') {
        statusBadge = `<span class="queue-status-chip error" title="${item.error || ''}">Failed</span>`;
      } else {
        statusBadge = '<span class="queue-status-chip">Queued</span>';
      }

      html += `
        <div class="queue-item">
          <div class="queue-item-info">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
            <span class="queue-item-name" title="${item.name}">${item.name}</span>
            <span class="queue-item-size">${item.size}</span>
          </div>
          ${statusBadge}
        </div>
      `;
    });

    this.queueListEl.innerHTML = html;
  }

  _formatBytes(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
  }
}

// ============================================================================
// 5. Lightbox & Bitcask EXIF Inspector Manager (with Touch Swipe)
// ============================================================================
class LightboxManager {
  constructor(app) {
    this.app = app;
    this.modalEl = document.getElementById('lightbox-modal');
    this.imgEl = document.getElementById('lightbox-img');
    this.spinnerEl = document.getElementById('lightbox-spinner');
    this.counterEl = document.getElementById('lightbox-counter');
    this.filenameEl = document.getElementById('lightbox-filename');
    this.drawerEl = document.getElementById('lightbox-drawer');
    this.downloadBtn = document.getElementById('lightbox-download-btn');
    this.infoToggleBtn = document.getElementById('lightbox-info-toggle-btn');
    this.stageEl = document.getElementById('lightbox-stage');

    this.photosList = [];
    this.currentIndex = -1;
    this.isDrawerOpen = false;

    // Mobile Touch Navigation State
    this.touchStartX = 0;
    this.touchStartY = 0;
    this.touchEndX = 0;

    this._initEvents();
  }

  _initEvents() {
    const closeBtn = document.getElementById('lightbox-close-btn');
    if (closeBtn) closeBtn.addEventListener('click', () => this.close());

    const prevBtn = document.getElementById('lightbox-prev-btn');
    if (prevBtn) prevBtn.addEventListener('click', () => this.prev());

    const nextBtn = document.getElementById('lightbox-next-btn');
    if (nextBtn) nextBtn.addEventListener('click', () => this.next());

    if (this.infoToggleBtn) {
      this.infoToggleBtn.addEventListener('click', () => this.toggleDrawer());
    }

    const drawerCloseBtn = document.getElementById('drawer-close-btn');
    if (drawerCloseBtn) {
      drawerCloseBtn.addEventListener('click', () => this.closeDrawer());
    }

    // Touch Swipe Navigation for Mobile Devices
    if (this.stageEl) {
      this.stageEl.addEventListener('touchstart', (e) => {
        this.touchStartX = e.changedTouches[0].screenX;
        this.touchStartY = e.changedTouches[0].screenY;
      }, { passive: true });

      this.stageEl.addEventListener('touchend', (e) => {
        this.touchEndX = e.changedTouches[0].screenX;
        const touchEndY = e.changedTouches[0].screenY;
        this._handleSwipeGesture(this.touchStartX, this.touchStartY, this.touchEndX, touchEndY);
      }, { passive: true });
    }

    // Copy to clipboard helpers
    document.querySelectorAll('.copy-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const targetId = btn.dataset.copyTarget;
        const targetEl = document.getElementById(targetId);
        if (targetEl && targetEl.textContent) {
          navigator.clipboard.writeText(targetEl.textContent.trim());
          this.app.toast.show('Copied to clipboard!', 'info', 2000);
        }
      });
    });

    // Keyboard navigation
    window.addEventListener('keydown', (e) => {
      if (this.modalEl.classList.contains('hidden')) return;

      if (e.key === 'Escape') {
        if (this.isDrawerOpen && window.innerWidth <= 768) {
          this.closeDrawer();
        } else {
          this.close();
        }
      } else if (e.key === 'ArrowLeft') {
        this.prev();
      } else if (e.key === 'ArrowRight') {
        this.next();
      } else if (e.key.toLowerCase() === 'i') {
        this.toggleDrawer();
      }
    });
  }

  _handleSwipeGesture(startX, startY, endX, endY) {
    const diffX = endX - startX;
    const diffY = endY - startY;
    // Require horizontal gesture dominance
    if (Math.abs(diffX) > Math.abs(diffY) && Math.abs(diffX) > 45) {
      if (diffX > 0) {
        this.prev(); // Swiped right -> go to previous
      } else {
        this.next(); // Swiped left -> go to next
      }
    }
  }

  open(photoId, photosList) {
    this.photosList = photosList || [];
    this.currentIndex = this.photosList.findIndex(p => p.photo_id === photoId);
    if (this.currentIndex === -1 && this.photosList.length > 0) {
      this.currentIndex = 0;
    }

    if (this.modalEl) this.modalEl.classList.remove('hidden');
    document.body.style.overflow = 'hidden';

    this.renderCurrent();
  }

  close() {
    if (this.modalEl) this.modalEl.classList.add('hidden');
    document.body.style.overflow = '';
    this.closeDrawer();
  }

  prev() {
    if (this.photosList.length === 0) return;
    this.currentIndex = (this.currentIndex - 1 + this.photosList.length) % this.photosList.length;
    this.renderCurrent();
  }

  next() {
    if (this.photosList.length === 0) return;
    this.currentIndex = (this.currentIndex + 1) % this.photosList.length;
    this.renderCurrent();
  }

  toggleDrawer() {
    this.isDrawerOpen = !this.isDrawerOpen;
    if (this.drawerEl) this.drawerEl.classList.toggle('open', this.isDrawerOpen);
    if (this.infoToggleBtn) this.infoToggleBtn.classList.toggle('active', this.isDrawerOpen);
  }

  closeDrawer() {
    this.isDrawerOpen = false;
    if (this.drawerEl) this.drawerEl.classList.remove('open');
    if (this.infoToggleBtn) this.infoToggleBtn.classList.remove('active');
  }

  async renderCurrent() {
    const photo = this.photosList[this.currentIndex];
    if (!photo) return;

    // Update Counter & Filename
    if (this.counterEl) {
      this.counterEl.textContent = `${this.currentIndex + 1} of ${this.photosList.length}`;
    }
    if (this.filenameEl) {
      this.filenameEl.textContent = `${photo.photo_id}.jpg`;
    }

    // Set Download link
    if (this.downloadBtn) {
      this.downloadBtn.href = `/photos/${photo.photo_id}/file`;
      this.downloadBtn.setAttribute('download', `${photo.photo_id}.jpg`);
    }

    // Load Image with Spinner
    if (this.spinnerEl) this.spinnerEl.classList.remove('hidden');
    if (this.imgEl) {
      this.imgEl.classList.add('hidden');
      this.imgEl.src = `/photos/${photo.photo_id}/file`;
      this.imgEl.onload = () => {
        if (this.spinnerEl) this.spinnerEl.classList.add('hidden');
        this.imgEl.classList.remove('hidden');
      };
      this.imgEl.onerror = () => {
        if (this.spinnerEl) this.spinnerEl.classList.add('hidden');
        this.app.toast.show('Failed to load full-resolution image', 'error');
      };
    }

    // Populate EXIF & Bitcask Inspector Data
    this._populateInspector(photo);
  }

  _populateInspector(photo) {
    const setText = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.textContent = val || '--';
    };

    setText('meta-camera', photo.camera || 'Standard Camera / None');
    
    // Dates
    const takenFormatted = photo.taken_at 
      ? new Date(photo.taken_at).toLocaleString() 
      : 'No EXIF Timestamp';
    setText('meta-taken-at', takenFormatted);

    const uploadedFormatted = photo.uploaded_at 
      ? new Date(photo.uploaded_at).toLocaleString() 
      : '--';
    setText('meta-uploaded-at', uploadedFormatted);

    // GPS & Map link
    const gpsEl = document.getElementById('meta-gps');
    const mapLinkEl = document.getElementById('meta-maps-link');
    if (photo.gps && Array.isArray(photo.gps) && photo.gps.length === 2) {
      const [lat, lon] = photo.gps;
      if (gpsEl) gpsEl.textContent = `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
      if (mapLinkEl) {
        mapLinkEl.href = `https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}#map=15/${lat}/${lon}`;
        mapLinkEl.classList.remove('hidden');
      }
    } else {
      if (gpsEl) gpsEl.textContent = 'None';
      if (mapLinkEl) mapLinkEl.classList.add('hidden');
    }

    // Bitcask Engine Details
    setText('meta-photo-id', photo.photo_id);
    setText('meta-content-hash', photo.content_hash);
    setText('meta-segment-id', photo.segment_id || 'Active Segment');
    setText('meta-offset-length', `Offset: ${photo.offset ?? 0} | Length: ${photo.length ?? 0} B`);
  }
}

// ============================================================================
// 6. Admin & Storage Maintenance Operations Manager
// ============================================================================
class AdminManager {
  constructor(app) {
    this.app = app;
    this.modalEl = document.getElementById('admin-modal');
    this.photoCountEl = document.getElementById('admin-photo-count');
    this.outputBox = document.getElementById('admin-output-box');
    this.outputConsole = document.getElementById('admin-output-console');

    this._initEvents();
  }

  _initEvents() {
    const openBtn = document.getElementById('admin-modal-btn');
    if (openBtn) openBtn.addEventListener('click', () => this.openModal());

    const closeBtn = document.getElementById('close-admin-modal-btn');
    if (closeBtn) closeBtn.addEventListener('click', () => this.closeModal());

    if (this.modalEl) {
      this.modalEl.addEventListener('click', (e) => {
        if (e.target === this.modalEl) this.closeModal();
      });
    }

    const compactBtn = document.getElementById('trigger-compact-btn');
    if (compactBtn) {
      compactBtn.addEventListener('click', () => this.runCompaction());
    }

    const snapshotBtn = document.getElementById('trigger-snapshot-btn');
    if (snapshotBtn) {
      snapshotBtn.addEventListener('click', () => this.saveSnapshot());
    }

    const clearOutputBtn = document.getElementById('clear-admin-output-btn');
    if (clearOutputBtn) {
      clearOutputBtn.addEventListener('click', () => {
        if (this.outputBox) this.outputBox.classList.add('hidden');
        if (this.outputConsole) this.outputConsole.textContent = '';
      });
    }
  }

  openModal() {
    if (this.modalEl) this.modalEl.classList.remove('hidden');
    if (this.photoCountEl) {
      this.photoCountEl.textContent = this.app.gallery.photos.length;
    }
  }

  closeModal() {
    if (this.modalEl) this.modalEl.classList.add('hidden');
  }

  async runCompaction() {
    this.app.toast.show('Starting segment compaction...', 'info');
    try {
      const res = await APIClient.compactSegments();
      this._logOutput('=== COMPACTION COMPLETE ===\n' + JSON.stringify(res, null, 2));
      this.app.toast.show(`Compacted ${res.segments_compacted} segment(s)!`, 'success');
    } catch (err) {
      this._logOutput('COMPACTION ERROR: ' + err.message);
      this.app.toast.show('Compaction failed: ' + err.message, 'error');
    }
  }

  async saveSnapshot() {
    this.app.toast.show('Generating in-memory index checkpoint...', 'info');
    try {
      const res = await APIClient.saveSnapshot();
      this._logOutput('=== SNAPSHOT SAVED ===\n' + JSON.stringify(res, null, 2));
      this.app.toast.show(`Snapshot checkpoint saved (${res.photos_in_index} photos).`, 'success');
    } catch (err) {
      this._logOutput('SNAPSHOT ERROR: ' + err.message);
      this.app.toast.show('Snapshot failed: ' + err.message, 'error');
    }
  }

  _logOutput(text) {
    if (this.outputBox) this.outputBox.classList.remove('hidden');
    if (this.outputConsole) {
      const timestamp = new Date().toLocaleTimeString();
      this.outputConsole.textContent = `[${timestamp}] ${text}`;
    }
  }
}

// ============================================================================
// 7. Main Application Orchestrator
// ============================================================================
class App {
  constructor() {
    this.toast = new ToastManager();
    this.gallery = new GalleryManager(this);
    this.uploader = new UploadManager(this);
    this.lightbox = new LightboxManager(this);
    this.admin = new AdminManager(this);

    this._initAppEvents();
    this._startHealthCheck();
  }

  _initAppEvents() {
    // Search Inputs (Desktop & Mobile)
    const searchInput = document.getElementById('search-input');
    const mobileSearchInput = document.getElementById('mobile-search-input');
    const clearSearchBtn = document.getElementById('search-clear-btn');

    const handleSearch = (val) => {
      this.gallery.setSearchQuery(val);
      if (clearSearchBtn) {
        clearSearchBtn.classList.toggle('hidden', !val);
      }
      if (searchInput && searchInput.value !== val) searchInput.value = val;
      if (mobileSearchInput && mobileSearchInput.value !== val) mobileSearchInput.value = val;
    };

    if (searchInput) {
      searchInput.addEventListener('input', (e) => handleSearch(e.target.value));
    }
    if (mobileSearchInput) {
      mobileSearchInput.addEventListener('input', (e) => handleSearch(e.target.value));
    }
    if (clearSearchBtn) {
      clearSearchBtn.addEventListener('click', () => handleSearch(''));
    }

    // Global Key Shortcuts
    window.addEventListener('keydown', (e) => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
      if (e.key.toLowerCase() === 'u') {
        this.uploader.openModal();
      } else if (e.key.toLowerCase() === 's') {
        e.preventDefault();
        if (searchInput) searchInput.focus();
      }
    });
  }

  clearSearch() {
    const searchInput = document.getElementById('search-input');
    const mobileSearchInput = document.getElementById('mobile-search-input');
    const clearSearchBtn = document.getElementById('search-clear-btn');
    if (searchInput) searchInput.value = '';
    if (mobileSearchInput) mobileSearchInput.value = '';
    if (clearSearchBtn) clearSearchBtn.classList.add('hidden');
    this.gallery.setSearchQuery('');
  }

  async _startHealthCheck() {
    const statusEl = document.getElementById('server-status');
    const labelEl = document.getElementById('status-label');

    const check = async () => {
      const isOnline = await APIClient.checkHealth();
      if (statusEl && labelEl) {
        statusEl.classList.toggle('online', isOnline);
        statusEl.classList.toggle('offline', !isOnline);
        labelEl.textContent = isOnline ? 'Online' : 'Offline';
      }
    };

    await check();
    setInterval(check, 10000);
  }

  init() {
    this.gallery.loadPhotos();
  }
}

// Bootstrap Application on DOM Ready
document.addEventListener('DOMContentLoaded', () => {
  const app = new App();
  app.init();
  window.__PIXEL_VAULT__ = app;
});

document.addEventListener("DOMContentLoaded", () => {
    const photoInput = document.getElementById("photoInput");
    const choosePhotoBtn = document.getElementById("choosePhotoBtn");
    const uploadPhotoBtn = document.getElementById("uploadPhotoBtn");
    const profilePicture = document.getElementById("profilePreview");
    const status = document.getElementById("photoUploadStatus");

    if (!photoInput || !choosePhotoBtn || !uploadPhotoBtn) return;

    let selectedFile = null;

    choosePhotoBtn.addEventListener("click", () => {
        photoInput.click();
    });

    photoInput.addEventListener("change", () => {
        const file = photoInput.files[0];

        if (!file) return;

        const allowedTypes = [
            "image/jpeg",
            "image/png",
            "image/webp"
        ];

        if (!allowedTypes.includes(file.type)) {
            status.textContent = "Please select a JPG, PNG, or WebP image.";
            return;
        }

        if (file.size > 5 * 1024 * 1024) {
            status.textContent = "Image size must be less than 5 MB.";
            return;
        }

        selectedFile = file;

        if (profilePicture) {
    profilePicture.src = URL.createObjectURL(file);
}
        uploadPhotoBtn.disabled = false;
        status.textContent = "Image selected. Click Upload Picture.";
    });

    uploadPhotoBtn.addEventListener("click", async () => {
        if (!selectedFile) return;

        uploadPhotoBtn.disabled = true;
        status.textContent = "Generating upload URL...";

        try {
            // 1. Request pre-signed URL from Flask in profile.py , here the data has has the type of the file to be uploaded.
            const urlResponse = await fetch("/profile/photo/upload-url", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({
                    contentType: selectedFile.type
                })
            });

            const uploadData = await urlResponse.json();

            if (!urlResponse.ok) {
                throw new Error(uploadData.error || "Could not generate upload URL.");
            }

            // 2. Upload image directly to S3.
            status.textContent = "Uploading image...";

            const s3Response = await fetch(uploadData.uploadUrl, {
                method: "PUT",
                headers: {
                    "Content-Type": uploadData.contentType
                },
                body: selectedFile
            });

            if (!s3Response.ok) {
                throw new Error("S3 image upload failed.");
            }

            // 3. Save photoKey using existing profile update API.
            status.textContent = "Saving profile picture...";

            const saveResponse = await fetch("/profile/photo/save", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({
                    photoKey: uploadData.photoKey
                })
            });

            const saveData = await saveResponse.json();

            if (!saveResponse.ok) {
                throw new Error(saveData.error || "Could not save profile picture.");
            }

            status.textContent = "Profile picture updated successfully!";
            selectedFile = null;
            photoInput.value = "";

            // Refresh the displayed image after saving.
            window.location.reload();

        } catch (error) {
            status.textContent = error.message;
            uploadPhotoBtn.disabled = false;
        }
    });
});